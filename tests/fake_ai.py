#!/usr/bin/env python3
"""Deterministic stand-in for an AI CLI (no network, no model). Reads a prompt on stdin.

Behaviour is selected by markers inside the commit message under analysis:
  [ai-fail]       exit 2 with a generic error on every call
  [ai-flaky]      fail with a transient error on the first FAKE_AI_FLAKY calls for this prompt (default 1), then succeed
  [ai-malformed]  print text that is not JSON on the first call, valid JSON afterwards (repair path)
  [ai-fabricate]  cite an unknown evidence id and a quote that is not in the evidence
  [ai-ratelimit]  exit 1 with a rate-limit message
  [ai-quota]      exit 1 with a quota message
  [ai-slow]       sleep far longer than any test timeout
  [ai-nothing]    reply that no intent was found
  [ai-inject]     (analysis behaves normally; used to confirm prompt text is only data)
  [ai-kind:K]     the finding kind is K (e.g. rejected-alternative); [ai-rel:R] makes the link relation R
Linking replies look at the new commit's message: "reverts" -> reverses, "replaces" -> supersedes,
"adjusts" -> refines; otherwise no links.
Every call is appended to $FAKE_AI_LOG (JSON lines) when set; prompts are saved under $FAKE_AI_DUMP when set.
"""
import hashlib
import json
import os
import re
import sys
import time

BLOCK = re.compile(r"<<<EVIDENCE id=(\S+) type=(\S+) nonce=(\w+)>>>\n(.*?)\n<<<END id=\1 nonce=\3>>>", re.S)


def log(entry):
    path = os.environ.get("FAKE_AI_LOG")
    if path:
        with open(path, "a") as stream:
            stream.write(json.dumps(entry) + "\n")


def count_call(key):
    directory = os.environ.get("FAKE_AI_STATE")
    if not directory:
        return 1
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, hashlib.sha256(key.encode()).hexdigest()[:20])
    n = 1 + (int(open(path).read()) if os.path.exists(path) else 0)
    with open(path, "w") as stream:
        stream.write(str(n))
    return n


def main():
    prompt = sys.stdin.read()
    items = {m.group(1): (m.group(2), m.group(4)) for m in BLOCK.finditer(prompt)}
    is_link = "You decide how NEW findings" in prompt
    message = ""
    if "C0" in items:
        text = items["C0"][1]
        message = text.partition("\n\n")[2] if "\n\n" in text else text
    log({"kind": "link" if is_link else "extract", "prompt_chars": len(prompt), "cwd": os.getcwd(),
         "env_has_token": any("TOKEN" in k and v for k, v in os.environ.items()), "args": sys.argv[1:],
         "marker_mentions_ignore": "ignore previous instructions" in prompt.lower()})
    dump = os.environ.get("FAKE_AI_DUMP")
    if dump:
        os.makedirs(dump, exist_ok=True)
        with open(os.path.join(dump, hashlib.sha256(prompt.encode()).hexdigest()[:16] + (".link" if is_link else ".extract") + ".txt"), "w") as stream:
            stream.write(prompt)
    nth = count_call(hashlib.sha256(message.encode()).hexdigest() + str(is_link))
    if "[ai-slow]" in message:
        time.sleep(60)
    if "[ai-fail]" in message:
        sys.stderr.write("fatal: model backend crashed\n")
        return 2
    if "[ai-ratelimit]" in message:
        sys.stderr.write("Error 429: rate limit exceeded, please retry later\n")
        return 1
    if "[ai-quota]" in message:
        sys.stderr.write("You have exceeded your quota for this billing period\n")
        return 1
    if "[ai-flaky]" in message and nth <= int(os.environ.get("FAKE_AI_FLAKY", "1")):
        sys.stderr.write("503 service unavailable: temporarily overloaded\n")
        return 1
    if "[ai-malformed]" in message and nth == 1:
        print("Sure! Here is the analysis you asked for, but I forgot the JSON.")
        return 0
    if is_link:
        findings = {r: t for r, (k, t) in items.items() if k == "new-finding"}
        links = []
        relation = "reverses" if "reverts" in message else "supersedes" if "replaces" in message else "refines" if "adjusts" in message else None
        quote = {"reverses": "reverts", "supersedes": "replaces", "refines": "adjusts"}.get(relation)
        marker = re.search(r"\[ai-rel:([a-z]+)\]", message)
        if marker:
            relation, quote = marker.group(1), marker.group(0)
        if relation and findings:
            links.append({"finding": sorted(findings)[0], "target": "N1", "relation": relation,
                          "evidence": [{"ref": "C0", "quote": quote}], "explanation": f"commit message says it {quote}"})
        print(json.dumps({"links": links}))
        return 0
    if "[ai-nothing]" in message:
        print(json.dumps({"no_intent_found": True, "findings": []}))
        return 0
    lines = [l for l in message.splitlines() if l.strip()]
    subject = lines[0] if lines else "change"
    paths = []
    for line in items.get("P0", ("", ""))[1].splitlines():
        m = re.match(r"FILE (\S+)", line)
        if m:
            paths.append({"path": m.group(1), "lines": None})
    kind = re.search(r"\[ai-kind:([a-z-]+)\]", message)
    finding = {"kind": kind.group(1) if kind else "design-decision", "title": re.sub(r"\[ai-[a-z:-]+\]", "", subject).strip()[:80] or "change",
               "statement": "The commit message states: " + subject, "basis": "explicit",
               "evidence": [{"ref": "C0", "quote": subject}], "uncertainty": "low", "uncertainty_notes": None, "tags": [],
               "paths": paths[:3], "change_hint": None, "missing_evidence": None}
    for ref, (kind_, text) in items.items():
        if kind_ == "forge-comment":
            body = [l.strip() for l in text.splitlines() if l.strip()][-1][:80]
            finding["evidence"].append({"ref": ref, "quote": body})
            break
    if "[ai-fabricate]" in message:
        finding["evidence"] = [{"ref": "Z99", "quote": "nothing like this"}, {"ref": "C0", "quote": "words never written anywhere"}]
        finding["paths"] = [{"path": "does/not/exist.py", "lines": None}]
    print(json.dumps({"no_intent_found": False, "findings": [finding]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
