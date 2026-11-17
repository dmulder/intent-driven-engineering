"""`intentctl trueup ...` command line. Optional: nothing else in intentctl imports this unless asked."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading

from . import ANALYSIS_VERSION, RECONSTRUCTED_LABEL, PROMPT_VERSION, STATE_FORMAT
from . import analysis, engine as engine_mod, forge as forge_mod, gitscan, graph as graph_mod, query as query_mod
from .providers import (BUILTIN_PROVIDERS, Budget, Caller, Circuits, build_providers)
from .store import Store, default_state_dir
from .importer import import_history
from .util import FatalStateError, TrueUpError, dumps, iso, now, read_json

EXAMPLES = """examples:
  intentctl trueup init                          # freeze HEAD's history into a manifest (no AI, no network)
  intentctl trueup estimate                      # scale of a full run, without calling anything
  intentctl trueup run --no-ai                   # inspect every commit locally (free), resumable
  intentctl trueup run --provider claude --acknowledge-data-sharing --max-commits 20   # small bounded trial
  intentctl trueup run --provider claude --provider gemini --acknowledge-data-sharing --jobs 4
  intentctl trueup status                        # coverage: inspection / AI / external evidence / discoveries
  intentctl trueup failures && intentctl trueup retry --stage analysis
  intentctl trueup query src/auth.py:42          # retrospective claims for a location (also shown by `explain`)
  intentctl explain src/auth.py:42 --json         # ordinary canonical history, including reconstructed context
  git add .intent && intentctl validate --staged  # commit the imported intent data
"""


def say(message):
    print(message, file=sys.stderr, flush=True)


def emit(value, as_json):
    if as_json:
        sys.stdout.write(dumps(value).decode())
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False))


def common(p):
    p.add_argument("--repo", help="path of the Git repository to read (default: the repository containing the current directory)")
    p.add_argument("--cache-dir", "--state-dir", dest="state_dir", help="processing cache (default: .intent/cache/trueup); results always go into .intent")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def parser():
    top = argparse.ArgumentParser(prog="intentctl trueup", description=(
        "Reconstruct project history as actual .intent records, with evidence, uncertainty and pending verification. "
        "Import preserves Git history and source files; processing checkpoints live in ignored .intent/cache/trueup/."),
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(dest="trueup_command", required=True)

    def add(name, help_, **kw):
        p = sub.add_parser(name, help=help_, description=help_, **kw)
        common(p)
        return p

    p = add("init", "freeze the commit scope into a persistent manifest; no AI or network is used")
    p.add_argument("--ref", action="append", help="starting ref (repeatable); default HEAD")
    p.add_argument("--all-refs", action="store_true", help="include every local branch and tag")
    p.add_argument("--include-remotes", action="store_true", help="with --all-refs, also remote-tracking branches")
    p.add_argument("--forge", action="append", help="KIND:REPO_URL[,api=URL][,token-env=NAME][,alias] (github or gitlab); remotes are auto-detected")
    p.add_argument("--network", choices=["off", "forge"], default="off", help="default network policy recorded in the config (default off)")
    p.add_argument("--config", help="JSON file with providers, limits, forge limits, ai defaults")

    p = add("extend", "append commits reachable from new/moved refs to the frozen scope (existing order is preserved)")
    p.add_argument("--ref", action="append")
    p.add_argument("--all-refs", action="store_true")
    p.add_argument("--include-remotes", action="store_true")

    p = add("estimate", "estimate the size of a full run from local Git data only; calls nothing external")
    p.add_argument("--bytes-per-changed-line", type=int, default=60, help="assumption used to convert changed lines to prompt characters")

    p = add("run", "process pending commits; resumable and idempotent (use --max-commits for a small trial)")
    p.add_argument("--no-ai", action="store_true", help="inspection (and optional forge evidence) only; no model calls")
    p.add_argument("--dry-run", action="store_true", help="analyse into the disposable cache without importing .intent records")
    p.add_argument("--provider", action="append", help="ordered AI provider (repeatable); fallback happens only through this list. Built-ins: " + ", ".join(sorted(BUILTIN_PROVIDERS)))
    p.add_argument("--provider-path", action="append", metavar="NAME=PATH", help="override a provider's executable")
    p.add_argument("--model", action="append", metavar="NAME=MODEL", help="model for a provider (where its CLI supports one)")
    p.add_argument("--acknowledge-data-sharing", action="store_true",
                   help="required for non-local providers: confirms redacted commit text, diffs and forge discussions are sent to them")
    p.add_argument("--network", choices=["off", "forge"], help="override the stored network policy for this run")
    p.add_argument("--allow-insecure-http", action="store_true", help="allow sending forge credentials over plain http (never for remote hosts by default)")
    p.add_argument("--forge-association", choices=["auto", "always", "never"])
    p.add_argument("--forge-depth", type=int)
    p.add_argument("--max-forge-requests", type=int, default=0)
    p.add_argument("--jobs", type=int, default=1, help="concurrent commits (default 1)")
    p.add_argument("--max-commits", type=int, default=0, help="stop after starting this many commits (trial runs)")
    p.add_argument("--max-ai-calls", type=int, default=0)
    p.add_argument("--max-input-mb", type=float, default=0, help="stop after sending this many megabytes of prompt text")
    p.add_argument("--max-cost-usd", type=float, default=0, help="stop once PROVIDER-REPORTED cost reaches this; providers that report no cost are not limited")
    p.add_argument("--timeout", type=int, default=0, help="seconds per AI call (default per provider)")
    p.add_argument("--retries", type=int, default=0, help="attempts per provider per request (default 3)")
    p.add_argument("--backoff-base", type=float, default=None, help="seconds; exponential backoff base with jitter (default 2)")
    p.add_argument("--only", action="append", metavar="SHA", help="limit to these commits (repeatable; must be in the manifest)")
    p.add_argument("--reset-circuits", action="store_true", help="forget persisted provider/forge outages before starting")
    p.add_argument("--accept-version-change", action="store_true", help="continue a store created by a different analysis version")

    p = add("status", "coverage summary: inspection, AI analysis, external evidence, linking, discoveries")
    p = add("report", "audit report with every incomplete item and the run history")
    p.add_argument("--limit", type=int, default=200)
    p = add("failures", "list commits with failed, partial or pending work")
    p.add_argument("--limit", type=int, default=200)

    p = sub.add_parser("retry", help="reset failed/incomplete items and run them again (completed work is reused)")
    common(p)
    p.add_argument("--stage", action="append", choices=["inspection", "external", "analysis", "link"])
    p.add_argument("--partial", action="store_true", help="also retry partially analysed commits")
    p.add_argument("--commit", action="append", help="limit to these commits (repeatable)")
    p.add_argument("--relink-dangling", action="store_true", help="re-run linking for commits whose link targets were reanalysed")
    _run_options(p)

    p = sub.add_parser("reanalyze", help="discard nothing, but run fresh AI analysis for chosen commits (new attempt appended)")
    common(p)
    p.add_argument("commits", nargs="+")
    _run_options(p)

    p = add("providers", "show configured AI providers, availability and outage state")
    p.add_argument("--provider", action="append")
    p.add_argument("--reset-circuits", action="store_true")

    p = add("show", "show an imported canonical intent (I-...) or cached commit diagnostics")
    p.add_argument("id")
    p.add_argument("--detail", action="store_true", help="include full quotes and link evidence")
    p.add_argument("--at", help="snapshot for present-day reconciliation (default HEAD)")

    p = add("list", "list reconstructed canonical intents")
    p.add_argument("--path")
    p.add_argument("--kind", choices=analysis.KINDS)
    p.add_argument("--lifecycle", choices=["active-in-history", "superseded", "reversed", "retired"])
    p.add_argument("--commit")
    p.add_argument("--reference", help="forge resource key, e.g. github:github.com/o/r#12")
    p.add_argument("--contains", help="case-insensitive text in title/statement")
    p.add_argument("--limit", type=int, default=50)

    p = add("tree", "plain-text evolution of discoveries and their relations")
    p.add_argument("--limit", type=int, default=100)

    p = add("query", "canonical intent and reconstructed historical context for PATH:LINE")
    p.add_argument("location")
    p.add_argument("--at", help="query this revision instead of HEAD")
    p.add_argument("--detail", action="store_true")

    p = add("graph-rebuild", "rebuild the internal processing graph from cached findings and links")
    p = add("verify", "check state integrity (manifest, records, orphan temp files); --repair removes only crash leftovers and rebuilds the graph")
    p.add_argument("--repair", action="store_true")
    return top


def _run_options(p):
    for flag, kw in (("--dry-run", {"action": "store_true"}), ("--no-ai", {"action": "store_true"}), ("--provider", {"action": "append"}), ("--provider-path", {"action": "append"}),
                     ("--model", {"action": "append"}), ("--acknowledge-data-sharing", {"action": "store_true"}),
                     ("--network", {"choices": ["off", "forge"]}), ("--allow-insecure-http", {"action": "store_true"}),
                     ("--forge-association", {"choices": ["auto", "always", "never"]}), ("--forge-depth", {"type": int}),
                     ("--max-forge-requests", {"type": int, "default": 0}), ("--jobs", {"type": int, "default": 1}),
                     ("--max-commits", {"type": int, "default": 0}), ("--max-ai-calls", {"type": int, "default": 0}),
                     ("--max-input-mb", {"type": float, "default": 0}), ("--max-cost-usd", {"type": float, "default": 0}),
                     ("--timeout", {"type": int, "default": 0}), ("--retries", {"type": int, "default": 0}),
                     ("--backoff-base", {"type": float, "default": None}), ("--reset-circuits", {"action": "store_true"}),
                     ("--accept-version-change", {"action": "store_true"})):
        p.add_argument(flag, **kw)
    p.set_defaults(only=None)


# ---------------------------------------------------------------------------------------------


def open_repo(args):
    return gitscan.Repo(args.repo or os.getcwd())


def state_path(repo, args):
    return Path(args.state_dir) if args.state_dir else default_state_dir(repo)


def load_config_file(path):
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise TrueUpError(f"cannot read --config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise TrueUpError("--config must contain a JSON object")
    unknown = set(data) - {"providers", "limits", "forge", "ai"}
    if unknown:
        raise TrueUpError(f"unknown config keys: {', '.join(sorted(unknown))}")
    return data


def cmd_init(args):
    repo = open_repo(args)
    path = state_path(repo, args)
    if Store.exists(path):
        raise TrueUpError(f"already initialized at {path}")
    scope = gitscan.build_scope(repo, args.ref, args.all_refs, args.include_remotes)
    detected, notes = forge_mod.detect_forges(repo)
    forges = detected + [forge_mod.parse_forge_spec(s) for s in (args.forge or [])]
    if args.forge:  # an explicit forge replaces detection's role assignment: first explicit is primary
        for f in forges:
            f["role"] = "alias"
        explicit = forges[len(detected):]
        explicit[0]["role"] = "primary"
    file_config = load_config_file(args.config)
    if path == default_state_dir(repo):
        ignore = repo.toplevel / ".intent/.gitignore"
        ignore.parent.mkdir(parents=True, exist_ok=True)
        if not ignore.exists():
            ignore.write_text("drafts/\ncache/\n")
    config = {"format": STATE_FORMAT, "created_at": iso(), "analysis_version": ANALYSIS_VERSION, "prompt_version": PROMPT_VERSION,
              "limits": {**analysis.DEFAULT_LIMITS, **file_config.get("limits", {})},
              "forge": {"network": args.network, "forges": forges, "limits": file_config.get("forge", {}).get("limits", {}),
                        "detection_notes": notes},
              "providers": file_config.get("providers", {}), "ai": file_config.get("ai", {})}
    store = Store.create(path, repo, config)
    try:
        manifest = store.write_manifest({**scope, "previous_digest": None})
        store.event("init", commits=len(manifest["commits"]), digest=manifest["digest"])
    finally:
        store.release()
    cov = scope["coverage"]
    return {"state_dir": str(path), "commits": len(manifest["commits"]), "refs": scope["scope"]["refs"], "manifest_digest": manifest["digest"],
            "history_complete": cov["complete"], "diagnostics": cov["notes"],
            "skipped_refs": cov["skipped_refs"], "forges": forges, "forge_detection_notes": notes, "network_default": args.network,
            "next": "intentctl trueup estimate; then `run --no-ai` or `run --provider NAME --acknowledge-data-sharing --max-commits 20`"}


def cmd_extend(args):
    repo = open_repo(args)
    with Store.open(state_path(repo, args)) as store:
        manifest = store.manifest()
        previous = manifest["scope"]
        refs = args.ref or (None if previous["all_local"] else [r["name"] for r in previous["refs"]])
        scope = gitscan.build_scope(repo, refs, args.all_refs or previous["all_local"], args.include_remotes or previous["include_remotes"])
        have = {c["sha"] for c in manifest["commits"]}
        full = {c["sha"]: {"parents": c["parents"], "ts": c["ts"]} for c in scope["commits"]}
        new = {s: v for s, v in full.items() if s not in have}
        if not new:
            return {"added": 0, "note": "no new commits are reachable from the selected refs", "moved_refs": ref_moves(manifest, repo)}
        order = gitscan.topological_order({s: v for s, v in new.items()})
        delta = [{"sha": s, "parents": new[s]["parents"], "ts": new[s]["ts"]} for s in order]
        written = store.write_manifest({**{k: scope[k] for k in ("scope", "coverage", "ordering")}, "commits": delta, "previous_digest": manifest["digest"]})
        store.event("extend", added=len(delta), digest=written["digest"])
        return {"added": len(delta), "generation": written["generation"], "manifest_digest": written["digest"],
                "note": "new commits are appended after existing ones; earlier commit order and records are untouched"}


def ref_moves(manifest, repo):
    moved = []
    for ref in manifest["scope"]["refs"]:
        try:
            now_oid = gitscan.resolve_ref(repo, ref["name"])
        except TrueUpError:
            moved.append({"ref": ref["name"], "recorded": ref["oid"], "now": None})
            continue
        if now_oid != ref["oid"]:
            moved.append({"ref": ref["name"], "recorded": ref["oid"], "now": now_oid})
    return moved


def cmd_estimate(args):
    repo = open_repo(args)
    store = Store.open(state_path(repo, args))
    manifest, config = store.manifest(), store.config()
    limits = {**analysis.DEFAULT_LIMITS, **config["limits"]}
    shas = [c["sha"] for c in manifest["commits"]]
    lines = {}
    process = gitscan.subprocess.Popen(["git", *gitscan.GIT_GLOBAL_ARGS, "-C", repo.path, "diff-tree", "--stdin", "--root", "-r", "--numstat", "-M",
                                        "--no-ext-diff", "--no-textconv"], stdin=gitscan.subprocess.PIPE, stdout=gitscan.subprocess.PIPE,
                                       stderr=gitscan.subprocess.DEVNULL, env=gitscan.git_env())
    out, _ = process.communicate(("\n".join(shas) + "\n").encode())
    current = None
    for line in out.decode("utf-8", "replace").splitlines():
        if re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", line):
            current = line
            lines.setdefault(current, 0)
        elif current and "\t" in line:
            added, deleted = line.split("\t")[:2]
            lines[current] += (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
    est_chars = [min(limits["max_diff_bytes"], lines.get(s, 0) * args.bytes_per_changed_line) for s in shas]
    bundles = [max(1, math.ceil((c + 1500) / limits["max_prompt_chars"])) for c in est_chars]
    refs = 0
    if config["forge"]["network"] == "forge" or config["forge"]["forges"]:
        log = repo.run("log", "--no-walk=unsorted", "--stdin", "--format=%B%x01", input=("\n".join(shas) + "\n").encode(), timeout=900).decode("utf-8", "replace")
        refs = sum(len(forge_mod.extract_references(m, "commit-message")) for m in log.split("\x01"))
    total_chars = sum(est_chars)
    extraction = sum(bundles)
    merges = sum(1 for c in manifest["commits"] if len(c["parents"]) > 1)
    return {"commits": len(shas), "merge_commits": merges, "root_commits": sum(1 for c in manifest["commits"] if not c["parents"]),
            "changed_lines_total": sum(lines.values()), "estimated_prompt_characters": total_chars + 1500 * len(shas),
            "estimated_ai_calls": {"extraction_low": extraction, "plausible_high": extraction + len(shas) + extraction // 4,
                                   "note": "high adds one linking call per commit and some consolidation; commits with no candidates skip linking"},
            "estimated_forge_requests": {"references_in_messages": refs, "if_network_forge": f"about {refs * 3 + len(shas)} (3 per reference, plus 1 per commit with association=always)"},
            "assumptions": [f"{args.bytes_per_changed_line} characters of prompt per changed line", f"prompt budget {limits['max_prompt_chars']} characters per call",
                            "retries, repairs and splits add calls", "token cost depends on the provider and is not estimated"],
            "suggestion": "run a bounded trial first: intentctl trueup run --provider NAME --acknowledge-data-sharing --max-commits 20; its report "
                          "shows real calls and (when the provider reports it) usage"}


def parse_pairs(values, label):
    out = {}
    for value in values or []:
        name, sep, rest = value.partition("=")
        if not sep or not name or not rest:
            raise TrueUpError(f"{label} expects NAME=VALUE, got {value!r}")
        out[name] = rest
    return out


def prepare_run(args, store, repo, stop):
    config = store.config()
    if config.get("analysis_version") != ANALYSIS_VERSION and not args.accept_version_change:
        raise TrueUpError(f"this store was created with analysis version {config.get('analysis_version')} but this tool is version "
                          f"{ANALYSIS_VERSION}; pass --accept-version-change to continue (new attempts record the new version) or start a new --state-dir")
    manifest = store.manifest()
    missing = repo.missing_commits([c["sha"] for c in manifest["commits"]])
    scope_check = {"missing_commit_objects": len(missing), "moved_refs": ref_moves(manifest, repo)}
    if missing:
        say(f"warning: {len(missing)} commit(s) in the frozen scope are missing from the repository (history rewritten or pruned); they will be recorded as failed")
    if args.reset_circuits:
        store.write_circuits({"providers": {}, "forges": {}})
    persisted = store.circuits()
    ai_cfg = config.get("ai", {})
    names = args.provider or ai_cfg.get("providers") or []
    providers = []
    caller = None
    if not args.no_ai and not names:
        raise TrueUpError("no AI provider selected: pass --provider NAME (see --help), or use --no-ai for inspection only")
    if not args.no_ai:
        paths = {**ai_cfg.get("paths", {}), **parse_pairs(args.provider_path, "--provider-path")}
        models = {**ai_cfg.get("models", {}), **parse_pairs(args.model, "--model")}
        providers = build_providers(names, config.get("providers"), paths, models, args.timeout or ai_cfg.get("timeout"))
        if not all(p.local for p in providers) and not args.acknowledge_data_sharing:
            raise TrueUpError("AI analysis sends redacted commit messages, diffs, file excerpts and forge discussions to: "
                              + ", ".join(p.name for p in providers if not p.local) +
                              ". Review docs/trueup.md (privacy), then pass --acknowledge-data-sharing to proceed.")
        forge_tokens = [f["token_env"] for f in config["forge"]["forges"] if f.get("token_env")]
        for provider in providers:
            provider.env_remove = list(provider.env_remove) + forge_tokens
        unavailable = [p.name for p in providers if not p.available()]
        if len(unavailable) == len(providers):
            raise TrueUpError("none of the configured AI providers is installed/executable: " + ", ".join(unavailable) +
                              ". Install one, set --provider-path, or run with --no-ai")
        for name in unavailable:
            say(f"warning: provider {name} is not available and will be skipped")
        budget = Budget(args.max_ai_calls, int(args.max_input_mb * 1024 * 1024), int(args.max_cost_usd * 1_000_000))
        circuits = Circuits(persisted.get("providers"))
        caller = Caller(providers, budget, circuits, max_attempts=args.retries or ai_cfg.get("max_attempts", 3),
                        max_repairs=ai_cfg.get("max_repairs", 1),
                        backoff_base=args.backoff_base if args.backoff_base is not None else ai_cfg.get("backoff_base", 2.0),
                        cancel=stop, timeout=args.timeout or None, on_event=lambda kind, **kw: (store.event(kind, **kw), say(f"  ! {kind}: {kw}")))
        store.event("data-sharing-acknowledged", providers=[p.name for p in providers if not p.local])
    forge_cfg = config["forge"]
    network = (args.network or forge_cfg["network"]) == "forge"
    limits = {**forge_cfg.get("limits", {})}
    if args.forge_association:
        limits["association"] = args.forge_association
    if args.forge_depth is not None:
        limits["max_depth"] = args.forge_depth
    if args.max_forge_requests:
        limits["max_requests"] = args.max_forge_requests
    forge = forge_mod.ForgeEvidence(store, forge_cfg["forges"], limits, allow_insecure=args.allow_insecure_http, enabled=network,
                                    circuits=persisted.get("forges") or {})
    eng = engine_mod.Engine(store, repo, config, providers, caller, forge, jobs=args.jobs, stop=stop, out=say, limits=config["limits"])
    return eng, scope_check


def with_signals(stop, state):
    def handler(signum, frame):
        if stop.is_set():
            raise KeyboardInterrupt()
        state["signal"] = signum
        stop.set()
        say("\ninterrupt received: finishing durable writes and stopping (send again to abort immediately)")
    old = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            old[sig] = signal.signal(sig, handler)
    return old


def summarize_run(status, report, eng, scope_check):
    return {"status": status, "exit_code": exit_code_for(status, eng), "counters": {**eng.counters, "ai_calls": eng.caller.budget.calls if eng.caller else 0}, "stop_reason": eng.stop_reason,
            "ai_calls": eng.caller.budget.calls if eng.caller else 0,
            "usage_reported_by_providers": (eng.caller.budget.usage if eng.caller else None),
            "usage_note": "token/cost totals include only what providers actually reported; zero reports means unknown, not free",
            "disabled_providers": eng.caller.circuits.snapshot() if eng.caller else {}, "forge": dict(eng.forge.stats) if eng.forge else None,
            "scope_check": scope_check, "coverage": report,
            "complete": status == "complete",
            "next": next_steps(status, report)}


def exit_code_for(status, eng=None):
    return {"complete": 0, "budget_exhausted": 3, "trial_limit": 3, "incomplete": 4, "interrupted": 130, "fatal": 1}.get(status, 1)


def next_steps(status, report):
    steps = []
    if status == "interrupted":
        steps.append("re-run `intentctl trueup run` to resume; durable per-commit progress was kept")
    if status in ("budget_exhausted", "trial_limit"):
        steps.append("run was stopped by a limit; re-run `intentctl trueup run` to continue where it stopped")
    if report["incomplete_total"]:
        steps.append("`intentctl trueup failures` lists incomplete items; `intentctl trueup retry` reprocesses only those")
    if not report["manifest"]["history_complete"]:
        steps.append("history coverage is incomplete (shallow/missing objects); fetch full history and `trueup extend`")
    return steps


def cmd_run(args, prepare=None):
    repo = open_repo(args)
    store = Store.open(state_path(repo, args))
    store.acquire()
    stop, state = threading.Event(), {}
    old = with_signals(stop, state)
    try:
        store.cleanup_temp()
        eng, scope_check = prepare_run(args, store, repo, stop)
        if args.only:
            unknown = [s for s in args.only if s not in eng.items]
            if unknown:
                raise TrueUpError(f"--only: not in the frozen manifest: {', '.join(unknown)}")
        if prepare:
            prepare(eng)
        items = eng.select(shas=args.only)
        say(f"selected {len(items)} of {len(eng.manifest['commits'])} commits (ai={'yes' if eng.ai else 'no'}, forge network={'yes' if eng.network else 'no'}, jobs={args.jobs})")
        status, report = eng.run(items, max_commits=args.max_commits)
        query_mod.write_coverage_cache(store, report)
        result = summarize_run(status, report, eng, scope_check)
        if eng.ai and not args.dry_run:
            try:
                store.put("imports", "latest", {"status": "pending"})
                result["import"] = import_history(repo, graph_mod.load_graph(store), eng.manifest, report,
                                                   eng.limits["max_diff_bytes"])
                result["import"]["status"] = "complete"
                store.put("imports", "latest", result["import"])
                result["next"].append("git add .intent && intentctl validate --staged, then commit the imported history")
            except Exception as exc:
                from ..core import IntentError
                if not isinstance(exc, (IntentError, TrueUpError)):
                    raise
                result.update(status="incomplete", complete=False, exit_code=4,
                              import_error=f"canonical historical import failed: {exc}")
                store.put("imports", "latest", {"status": "failed", "error": result["import_error"]})
                result["next"].append("fix the import error and rerun; analysis checkpoints are retained")
        run_record = read_json(store.path / "runs" / (eng.run_id + ".json"))
        run_record.update(status=result["status"], canonical_import=result.get("import"), import_error=result.get("import_error"))
        store.write_run(eng.run_id, run_record)
        if state.get("signal") == signal.SIGTERM:
            result["exit_code"] = 143
        return result
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
        store.release()


def cmd_retry(args):
    stages = tuple(args.stage or ("inspection", "external", "analysis", "link"))

    def prepare(eng):
        if args.relink_dangling:
            for d in graph_mod.load_graph(eng.store).dangling:
                rec = eng.store.commit(d["commit"])
                if rec and rec["link"]["status"] == "linked":
                    rec["link"] = {"status": "stale", "reason": "link target was reanalysed"}
                    eng.store.put_commit(rec)
        bad = ("failed", "partial") if args.partial else ("failed",)
        wanted = set(args.commit) if args.commit else None
        for item in eng.manifest["commits"]:
            if wanted is not None and item["sha"] not in wanted:
                continue
            rec = eng.store.commit(item["sha"])
            if not rec:
                continue
            changed = False
            for stage in stages:
                if stage == "link":
                    applicable = eng.ai and rec["link"]["status"] in ("failed", "stale")
                else:
                    applicable = rec[stage]["status"] in bad and (stage == "inspection" or (stage == "external" and eng.network) or (stage == "analysis" and eng.ai))
                if not applicable:
                    continue
                rec[stage] = {"status": "pending", "previous": rec[stage]["status"], "previous_error": rec[stage].get("error")}
                changed = True
            if eng.ai and rec["analysis"].get("class") == "inspection_failed" and rec["inspection"]["status"] == "pending":
                rec["analysis"] = {"status": "pending", "previous": "failed"}
                changed = True
            if changed:
                eng.store.put_commit(rec)
    args.only = args.commit
    return cmd_run(args, prepare=prepare)


def cmd_reanalyze(args):
    def prepare(eng):
        for sha_ in args.commits:
            if sha_ not in eng.items:
                raise TrueUpError(f"{sha_}: not in the frozen manifest")
            rec = eng.store.commit(sha_) or engine_mod.blank_commit(eng.items[sha_])
            rec["epoch"] = rec.get("epoch", 0) + 1
            previous = rec["analysis"].get("attempt")
            rec["analysis"] = {"status": "pending", "previous_attempt": previous, "reason": "reanalysis requested"}
            rec["link"] = {"status": "pending"}
            eng.store.put_commit(rec)
            eng.store.event("reanalyze", commit=sha_, epoch=rec["epoch"])
    args.only = args.commits
    return cmd_run(args, prepare=prepare)


def reader(args, need_manifest=True):
    repo = open_repo(args)
    store = Store.open(state_path(repo, args))
    return repo, store


def cmd_status(args):
    repo, store = reader(args)
    manifest = store.manifest()
    report = engine_mod.coverage_report(store, manifest)
    query_mod.write_coverage_cache(store, report)
    runs = store.runs()
    last = runs[-1] if runs else None
    import_state = store.get("imports", "latest", default={"status": "not_requested"})
    import_incomplete = import_state["status"] in ("pending", "failed")
    return {"label": RECONSTRUCTED_LABEL, "import": import_state, "complete": report["complete"] and not import_incomplete,
            "state_dir": str(store.path), "store": store.meta()["store_id"], "coverage": report,
            "last_run": last, "moved_refs": ref_moves(manifest, repo), "circuits": store.circuits(),
            "warnings": (["the last run never finished (crash or kill); durable progress was kept, re-run to resume"] if last and last.get("status") in ("running", "crashed_or_killed") else [])
            + (["Canonical import incomplete: " + import_state.get("error", "interrupted import; rerun to resume")] if import_incomplete else [])
            + ([] if report["complete"] else ["NOT COMPLETE: see coverage.incomplete_items; a partial run is never reported as success"])}


def cmd_report(args):
    repo, store = reader(args)
    manifest = store.manifest()
    report = engine_mod.coverage_report(store, manifest, list_limit=args.limit)
    runs = store.runs()
    usage = {"ai_calls": sum(r.get("ai_calls", 0) for r in runs), "reported": {}}
    for run in runs:
        for k, v in (run.get("usage_reported") or {}).items():
            usage["reported"][k] = usage["reported"].get(k, 0) + v
    return {"label": RECONSTRUCTED_LABEL, "scope": manifest["scope"], "history_coverage": manifest["coverage"], "manifest_generations": manifest["generations"],
            "ordering": manifest["ordering"], "coverage": report, "runs": runs[-20:], "usage_across_runs": usage,
            "usage_note": "only provider-reported figures are summed; unreported usage is unknown",
            "graph": graph_state(store)}


def graph_state(store):
    graph = graph_mod.load_graph(store)
    return {"nodes": len(graph.nodes), "built_at": getattr(graph, "built_at", None), "dangling_links": graph.dangling[:20],
            "merge_gate_rejections": graph.gate_rejections[:20]}


def cmd_failures(args):
    repo, store = reader(args)
    report = engine_mod.coverage_report(store, list_limit=args.limit)
    items = []
    for entry in report["incomplete_items"]:
        rec = store.commit(entry["sha"]) or {}
        items.append({**entry, "errors": (rec.get("errors") or [])[-3:]})
    return {"incomplete_total": report["incomplete_total"], "shown": len(items), "items": items,
            "hint": "`intentctl trueup retry [--stage S] [--partial]` retries only these; `reanalyze SHA` forces a fresh AI pass"}


def cmd_providers(args):
    repo, store = reader(args)
    config = store.config()
    names = args.provider or config.get("ai", {}).get("providers") or sorted(BUILTIN_PROVIDERS)
    if args.reset_circuits:
        store.acquire()
        store.write_circuits({"providers": {}, "forges": {}})
        store.release()
    providers = build_providers(names, config.get("providers"))
    return {"providers": [p.describe() for p in providers], "persisted_outages": store.circuits(),
            "builtin_templates": {k: [i["argv"] for i in v["invocations"]] for k, v in BUILTIN_PROVIDERS.items()},
            "note": "availability only checks that the executable exists; no model call is made"}


def cmd_show(args):
    repo = open_repo(args)
    if re.fullmatch(r"[0-9a-f]{7,64}", args.id):
        _, store = reader(args)
        hits = [c["sha"] for c in store.manifest()["commits"] if c["sha"].startswith(args.id)]
        if len(hits) != 1:
            raise TrueUpError(f"{args.id!r} matches {len(hits)} commits")
        return {"commit": hits[0], "record": store.commit(hits[0]), "link": store.get("links", hits[0]),
                "attempts": store.list_nested("attempts", hits[0])}
    return query_mod.show(repo, args.id, args.at)


def cmd_list(args):
    return query_mod.list_intents(open_repo(args), args)


def cmd_tree(args):
    return query_mod.tree(open_repo(args), args.limit)


def cmd_query(args):
    return query_mod.query(open_repo(args), args.location, args.at)


def cmd_graph_rebuild(args):
    repo, store = reader(args)
    store.acquire()
    try:
        eng = engine_mod.Engine(store, repo, store.config())
        graph = graph_mod.fold(eng._entries())
        graph_mod.write_graph(store, graph)
        report = engine_mod.coverage_report(store)
        query_mod.write_coverage_cache(store, report)
    finally:
        store.release()
    return {"nodes": len(graph.nodes), "dangling_links": graph.dangling, "merge_gate_rejections": len(graph.gate_rejections)}


def cmd_verify(args):
    repo, store = reader(args)
    issues = []
    manifest = store.manifest()
    for item in manifest["commits"]:
        rec = store.commit(item["sha"])
        if rec is None:
            continue
        attempt = rec["analysis"].get("attempt")
        if attempt and store.get_nested("attempts", item["sha"], f"{attempt:06d}") is None:
            issues.append({"sha": item["sha"], "problem": "analysis attempt file missing for a recorded attempt"})
        if rec["analysis"]["status"] in ("analyzed", "partial") and not attempt:
            issues.append({"sha": item["sha"], "problem": "analysis marked complete without an attempt"})
    temps = [str(p.relative_to(store.path)) for p in store.path.rglob(".tmp-*")]
    runs = [r["id"] for r in store.runs() if r.get("status") in ("running", "crashed_or_killed")]
    result = {"commit_records_checked": len(manifest["commits"]), "issues": issues, "orphan_temp_files": temps, "unfinished_runs": runs,
              "ok": not issues}
    if args.repair:
        store.acquire()
        try:
            result["removed_temp_files"] = store.cleanup_temp()
            eng = engine_mod.Engine(store, repo, store.config())
            graph_mod.write_graph(store, graph_mod.fold(eng._entries()))
            result["graph_rebuilt"] = True
        finally:
            store.release()
    return result


HANDLERS = {"init": cmd_init, "extend": cmd_extend, "estimate": cmd_estimate, "run": cmd_run, "status": cmd_status, "report": cmd_report,
            "failures": cmd_failures, "retry": cmd_retry, "reanalyze": cmd_reanalyze, "providers": cmd_providers, "show": cmd_show,
            "list": cmd_list, "tree": cmd_tree, "query": cmd_query,
            "graph-rebuild": cmd_graph_rebuild, "verify": cmd_verify}


def render_human(command, result):
    if command == "status":
        c = result["coverage"]
        lines = [result["label"], "", f"processing cache: {result['state_dir']}", f"commits in scope: {c['commits_in_scope']}  (history complete: {c['manifest']['history_complete']})"]
        lines += [f"  note: {n}" for n in c["manifest"]["notes"]]
        for name, key in (("inspection", "inspection"), ("AI analysis", "ai_analysis"), ("external evidence", "external_evidence"), ("linking", "linking")):
            lines.append(f"{name}: " + ", ".join(f"{k}={v}" for k, v in c[key].items() if v))
        d = c["discoveries"]
        lines.append(f"discoveries: {d['nodes']} nodes / {d['findings']} findings ({d['explicit']} explicit, {d['inferred']} inferred; "
                     f"{d['ended_in_history']} ended in history, {d['contested']} contested)")
        lines.append(f"COMPLETE: {c['complete']}  (incomplete items: {c['incomplete_total']})")
        lines += [f"warning: {w}" for w in result["warnings"]]
        return "\n".join(lines)
    if command == "run":
        c = result["coverage"]
        lines = [f"run status: {result['status']} (exit {result['exit_code']})", f"counters: {result['counters']}", f"AI calls: {result['ai_calls']}"]
        if result["stop_reason"]:
            lines.append(f"stopped: {result['stop_reason']}")
        lines.append(f"coverage: inspection {c['inspection']}")
        lines.append(f"          AI analysis {c['ai_analysis']}")
        lines.append(f"          external {c['external_evidence']}")
        lines.append(f"incomplete items: {c['incomplete_total']}; fully complete: {c['complete']}")
        lines += ["next: " + s for s in result["next"]]
        return "\n".join(lines)
    return None


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = HANDLERS[args.trueup_command](args)
    except FatalStateError as exc:
        say(f"fatal: {exc}")
        say("previously durable work is preserved; fix the cause and re-run to resume")
        return 1
    except TrueUpError as exc:
        say(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        say("aborted")
        return 130
    code = 0
    if isinstance(result, dict) and "exit_code" in result and args.trueup_command in ("run", "retry", "reanalyze"):
        code = result["exit_code"]
    if args.trueup_command == "verify" and isinstance(result, dict) and not result.get("ok", False):
        code = 1
    if args.json:
        emit({"text": result} if isinstance(result, str) else result, True)
    else:
        text = render_human(args.trueup_command, result)
        emit(text if text is not None else result, False)
    return code
