"""Review decisions and the explicit promotion transaction into canonical intent.

Promotion is the ONLY path from the true-up store into `.intent/`. It reuses the existing
capture / intent-create / bind / declare / finalize workflow, so the proposed records face the
normal validator. It never edits the baseline or committed records, never marks anything
independently verified, creates only a metadata-only event, and leaves staging and committing to
the maintainer. Bindings default to `historical` status, which cannot block future edits;
an `active` binding (a present-day constraint) needs an explicit flag and a stated reason.
"""
import argparse
import re

from . import graph as graph_mod
from .query import suggest_bindings
from .util import TrueUpError, iso

DECISIONS = ("approve", "reject", "defer")
END_TO_STATUS = {"superseded": "superseded", "reversed": "retired", "retired": "retired"}


def review_path_key(node_id):
    return node_id


def record_review(store, node, decision, reviewer, note):
    if decision not in DECISIONS:
        raise TrueUpError(f"decision must be one of {', '.join(DECISIONS)}")
    if not reviewer or not reviewer.strip():
        raise TrueUpError("--reviewer NAME is required: reviews are attributed, local, and unauthenticated")
    history = store.get("reviews", node["id"], default={"node": node["id"], "history": []})
    entry = {"decision": decision, "reviewer": reviewer.strip(), "note": note or "", "at": iso(), "node_hash": graph_mod.node_hash(node)}
    history["history"].append(entry)
    store.put("reviews", node["id"], history)
    store.event("review", node=node["id"], decision=decision, reviewer=reviewer.strip())
    return entry


def current_review(store, node):
    history = store.get("reviews", node["id"], default={"history": []})["history"]
    return history[-1] if history else None


def parse_bind(spec):
    """PATH or PATH:START-END (one-based inclusive lines at the staged snapshot)."""
    match = re.match(r"^(?P<path>.+?)(?::(?P<a>\d+)-(?P<z>\d+))?$", spec)
    if not match:
        raise TrueUpError(f"invalid --bind {spec!r}; expected PATH or PATH:FIRST-LAST")
    a, z = match.group("a"), match.group("z")
    return match.group("path"), (int(a), int(z)) if a else None


def byte_range(data, lines):
    if lines is None:
        return 0, len(data)
    first, last = lines
    starts = [0] + [i + 1 for i, b in enumerate(data) if b == 10]
    if first < 1 or last < first or first > len(starts) or last > len(starts):
        raise TrueUpError(f"line range {first}-{last} is outside the file ({len(starts)} lines)")
    start = starts[first - 1]
    end = starts[last] if last < len(starts) else len(data)
    return start, end


def intent_text(node, reviewer, note, store_id):
    findings = node["findings_data"] + node["supporting_data"]
    explicit = sum(1 for f in findings if f["basis"] == "explicit")
    sources = []
    for f in findings:
        for e in f["evidence"]:
            src = e.get("source") or {}
            label = src.get("url") or (f"commit {src['commit'][:12]}" if src.get("commit") else None)
            if label and label not in sources:
                sources.append(label)
    latest = node["findings_data"][-1]
    requirements = [f"[{f['basis']}; commit {f['commit'][:12]}] {f['statement']}" for f in node["findings_data"]
                    if f["kind"] in ("requirement", "invariant", "constraint")]
    constraints = [f"[{f['basis']}; commit {f['commit'][:12]}] Rejected alternative: {f['statement']}" for f in findings
                   if f["kind"] == "rejected-alternative"]
    rationale = (f"Retrospectively reconstructed by `intentctl trueup` from historical evidence (store {store_id}); it was NOT captured when "
                 f"the change was made and is NOT independently verified. {explicit} of {len(findings)} supporting finding(s) are backed by "
                 f"verified quotes, the rest are inferences. Historical status: {node['lifecycle']}. Evidence: {', '.join(sources[:8]) or 'see store'}. "
                 f"Promoted after review by {reviewer}" + (f": {note}" if note else "."))
    return {"title": ("Retrospective: " + node["title"])[:200], "description": latest["statement"], "rationale": rationale[:3000],
            "requirements": requirements, "constraints": constraints}


def promote(root, store, repo, node, args):
    """Run the reviewed transaction. Returns a JSON-able description; raises TrueUpError on any refused precondition."""
    from .. import cli as intent_cli
    from ..core import IntentError, ValidationError, changes_between, identity, metadata, record_path, safe_path, snapshot, source

    review = current_review(store, node)
    if not review or review["decision"] != "approve":
        raise TrueUpError("promotion requires a recorded `approve` review (intentctl trueup review NODE --decision approve --reviewer NAME)")
    digest = graph_mod.node_hash(node)
    if review["node_hash"] != digest:
        raise TrueUpError("the discovery changed after it was reviewed; review it again")
    if args.confirm != digest:
        raise TrueUpError(f"--confirm must equal the content hash you reviewed ({digest}); `trueup show {node['id']}` prints it")
    done = store.get("promotions", node["id"])
    if done and done.get("complete"):
        raise TrueUpError(f"this discovery was already promoted as intent {done['intent']} (key {done['key']!r}); revise that intent with the normal workflow")
    if not (root / ".intent/config.json").exists():
        raise TrueUpError("this repository has not adopted intentctl; run `intentctl init` (and `baseline`) first. True-up never adopts it implicitly")
    ended = node["lifecycle"] != graph_mod.ACTIVE
    status = "active" if not ended else END_TO_STATUS[node["lifecycle"]]
    binding_status = args.binding_status
    if ended and binding_status == "active":
        raise TrueUpError("this decision was ended in history; it can only be promoted with historical bindings")
    if binding_status == "active" and not args.applicability_rationale:
        raise TrueUpError("an active binding imposes a constraint on future edits; state why it applies today with --applicability-rationale")
    key = args.key or "trueup-" + node["id"][2:14]
    manifest = intent_cli.manifest(root)
    intent_id = identity(intent_cli.config(root), "intent", key)
    resume = bool(done and not done.get("complete") and done.get("node_hash") == digest
                  and done.get("key") == key and done.get("intent") == intent_id
                  and done.get("events") == [intent_cli.event_id(root, key)]
                  and done["events"][0] in manifest["events"])
    if resume:
        existing = intent_cli.read_json(root / record_path("intent", intent_id, 1))
        resume = existing["created_by_event"] == done["events"][0] and manifest["intents"].get(intent_id) == 1
    if intent_id in manifest["intents"] and not resume:
        raise TrueUpError(f"intent key {key!r} already exists in canonical history ({intent_id}); this discovery was already promoted")
    staged = snapshot(root, staged=True)
    if changes_between(intent_cli.pending_source(root), source(staged)):
        raise TrueUpError("staged source changes exist; promotion is a metadata-only transaction. Commit or unstage source first")
    event_key = key
    if not resume and intent_cli.draft_path(root, event_key).exists():
        raise TrueUpError(f"a draft for key {key!r} already exists (.intent/drafts); remove it or pass a distinct --key")
    text = intent_text(node, review["reviewer"], review["note"], store.meta()["store_id"])
    if args.title:
        text["title"] = args.title
    if args.requirement is not None:
        text["requirements"] = args.requirement
    if args.constraint is not None:
        text["constraints"] = args.constraint
    bindings = []
    for spec in args.bind or []:
        path, lines = parse_bind(spec)
        if not safe_path(path) or metadata(path):
            raise TrueUpError(f"--bind {path}: binding target must be a relative ordinary source path")
        entry = staged.get(path)
        if entry is None:
            raise TrueUpError(f"--bind {path}: not present in the index; true-up does not bind to files that are absent")
        start, end = byte_range(entry[1], lines)
        bindings.append({"path": path, "lines": lines, "start": start, "end": end})
    relationship = args.relationship or ("constrained-by" if binding_status == "active" else "historically-derived-from")
    from ..schema import SCHEMAS
    if relationship not in SCHEMAS["binding"]["properties"]["relationship"]["enum"]:
        raise TrueUpError(f"unsupported binding relationship {relationship!r}")
    for number in range(1, len(bindings) + 1):
        binding_key = f"{key}-b{number}"
        if identity(intent_cli.config(root), "binding", binding_key) in manifest["bindings"]:
            raise TrueUpError(f"binding key {binding_key!r} already exists; pass a distinct --key before any event is written")
    if resume:
        if binding_status != "historical" or not bindings:
            raise TrueUpError("resume the incomplete promotion with historical bindings")
        if any(existing[field] != value for field, value in {**text, "status": status, "related_intents": args.related or []}.items()):
            raise TrueUpError("the canonical intent differs from this promotion; keep its reviewed text and key when resuming")
    plan = {"key": key, "intent": {**text, "status": status, "kind": "retrospective"}, "bindings": bindings,
            "binding_status": binding_status, "relationship": relationship, "verification": "pending (never verified by promotion)",
            "reviewer": review["reviewer"], "node": node["id"], "node_hash": digest, "provenance": "unknown",
            "metadata_only": True, "suggested_bindings_not_applied": suggest_bindings(repo, node, "HEAD") if not bindings else []}
    if args.dry_run:
        return {**plan, "dry_run": True}
    rationale = (f"Maintainer {review['reviewer']} reviewed a non-authoritative true-up discovery and chose to record it as canonical intent. "
                 f"{review['note'] or ''}").strip()
    attestation = {"kind": "retrospective-trueup", "reference": f"trueup:{store.meta()['store_id']}/{node['id']}@{digest}",
                   "result": f"NOT verified. AI-assisted reconstruction; reviewed for promotion by {review['reviewer']}."}
    binding_rationale = (args.applicability_rationale if binding_status == "active" else
                         "Reviewer-chosen association with a historical decision; does not claim the decision applies today.") \
        + " Reconstructed retrospectively; not independently verified."
    # The validator only lets an `introduce` carry an active binding. Historical bindings are therefore recorded by a
    # second, separate event (`unapply` is the existing action for non-governing applicability) after the intent exists.
    split = binding_status == "historical" and bool(bindings)
    transitions, events = [], list(done["events"]) if resume else []

    def run_event(event_key, desc, body):
        draft_path = intent_cli.draft_path(root, event_key)
        if resume and event_key == bind_key and draft_path.exists():
            old_draft = intent_cli.load_draft(root, event_key)
            event = old_draft["event"]
            if event["id"] != intent_cli.event_id(root, event_key) or event["kind"] != "retrospective-promotion" or event["description"] != desc or event["changes"]:
                raise TrueUpError("existing binding draft does not match this promotion; inspect it before retrying")
            # Keep failed proposals for inspection before rebuilding from corrected arguments.
            store.put("promotion-drafts", event["id"], old_draft)
            draft_path.unlink()
        intent_cli.capture(root, argparse.Namespace(key=event_key, description=desc, rationale=rationale, kind="retrospective-promotion", provenance="unknown"))
        body()
        draft = intent_cli.load_draft(root, event_key)
        draft["event"]["verification"].append(attestation)
        intent_cli.save_draft(root, event_key, draft)
        events.append(intent_cli.finalize(root, argparse.Namespace(key=event_key))["event"])

    def add_bindings(action):
        for number, binding in enumerate(bindings, 1):
            intent_cli.add_binding(root, argparse.Namespace(
                command="bind", event=bind_key if split else key, key=f"{key}-b{number}", intent=intent_id, intent_revision=1,
                relationship=relationship, predecessor=None, path=binding["path"], start=binding["start"], end=binding["end"],
                status=binding_status, rationale=binding_rationale, verification_status="pending",
                verification_method="retrospective true-up reconstruction (not independently verified)",
                verification_evidence=f"trueup node {node['id']} content {digest}"))
            binding_id = intent_cli.identity(intent_cli.config(root), "binding", f"{key}-b{number}")
            intent_cli.declare(root, argparse.Namespace(event=bind_key if split else key, intent=intent_id, binding=binding_id, action=action, provenance=None))
            transitions.append(binding_id)

    def intent_body():
        intent_cli.add_intent(root, argparse.Namespace(command="intent-create", event=key, key=key, status=status, kind="retrospective",
                                                      title=text["title"], description=text["description"], rationale=text["rationale"],
                                                      requirement=text["requirements"], constraint=text["constraints"], related=args.related or []))
        if split or not bindings:
            intent_cli.declare(root, argparse.Namespace(event=key, intent=intent_id, binding=None, action="introduce", provenance=None))
        else:
            add_bindings("introduce")

    bind_key = key + "-bind"
    if split and not resume and intent_cli.draft_path(root, bind_key).exists():
        raise TrueUpError(f"a draft for key {bind_key!r} already exists (.intent/drafts); remove it or pass a distinct --key")
    try:
        if not resume:
            run_event(key, f"Promote reviewed retrospective discovery {node['id']}", intent_body)
            store.put("promotions", node["id"], {"node": node["id"], "node_hash": digest, "key": key, "intent": intent_id, "events": list(events),
                                                 "bindings": [], "reviewer": review["reviewer"], "at": iso(), "complete": not split})
        if split:
            run_event(bind_key, f"Record reviewer-chosen historical associations for {node['id']}", lambda: add_bindings("unapply"))
    except (IntentError, ValidationError) as exc:
        detail = getattr(exc, "errors", None) or str(exc)
        done = f" Event(s) already finalized: {events}." if events else ""
        raise TrueUpError(f"canonical workflow refused the promotion (draft kept in .intent/drafts for inspection).{done} {detail}") from exc
    store.put("promotions", node["id"], {"node": node["id"], "node_hash": digest, "key": key, "intent": intent_id, "events": events,
                                         "bindings": transitions, "reviewer": review["reviewer"], "at": iso(), "complete": True})
    store.event("promoted", node=node["id"], intent=intent_id, events=events)
    return {**plan, "dry_run": False, "events": events, "intent_id": intent_id, "binding_ids": transitions,
            "next": "git add .intent && python3 tools/intentctl.py validate --staged, then commit: promotion is staged and committed by you"}
