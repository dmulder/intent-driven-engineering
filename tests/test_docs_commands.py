"""Docs-as-tests: Telos' own review found README examples drifting from its CLI."""
import re
import shlex
import unittest

from support import PROJECT
from intentlib.cli import parser
from intentlib.trueup.cli import parser as trueup_parser

FENCE = re.compile(r"```[a-z]*\n(.*?)```", re.S)
INLINE = re.compile(r"`((?:python3 tools/intentctl\.py|intentctl)(?: [^`\n]*)?)`")
STOP = {"|", "||", "&&", ";", ">", ">>", "<"}
DOCS = ["README.md", "AGENTS.md", ".intent/README.md", *sorted(f"docs/{p.name}" for p in (PROJECT / "docs").glob("*.md"))]


def invocations(text):
    lines = []
    for block in FENCE.findall(text):
        lines += re.sub(r"\\\n\s*", " ", block).splitlines()
    lines += INLINE.findall(text)
    for line in lines:
        if "intentctl" not in line or line.lstrip().startswith("#"):
            continue
        tokens = shlex.split(line)
        starts = [i for i, t in enumerate(tokens) if t == "intentctl" or t.endswith("tools/intentctl.py")]
        if not starts:
            continue
        start = starts[0]
        rest = []
        for token in tokens[start + 1:]:
            if token in STOP:
                break
            rest.append(token)
        if rest:
            yield line.strip(), rest


class DocumentedCommandTests(unittest.TestCase):
    def setUp(self):
        self.commands = dict(parser()._subparsers._group_actions[0].choices)
        self.commands["trueup"] = trueup_parser()

    def test_documented_subcommands_and_flags_exist(self):
        found = 0
        for name in DOCS:
            for line, rest in invocations((PROJECT / name).read_text()):
                found += 1
                self.assertIn(rest[0], self.commands, f"{name}: unknown subcommand in: {line}")
                selected = self.commands[rest[0]]
                known = set(selected._option_string_actions)
                for token in rest[1:]:
                    groups = selected._subparsers._group_actions if selected._subparsers else []
                    nested = {name: sub for group in groups for name, sub in group.choices.items()}
                    if token in nested:
                        selected = nested[token]
                        known.update(selected._option_string_actions)
                for token in rest[1:]:
                    if token.startswith("--"):
                        self.assertIn(token.split("=")[0], known, f"{name}: unknown flag in: {line}")
        self.assertGreater(found, 25, "documentation scan found too few commands; the extractor may have rotted")

    def test_every_subcommand_is_documented_for_agents_or_users(self):
        text = "\n".join((PROJECT / name).read_text() for name in ("README.md", "AGENTS.md"))
        documented = {rest[0] for _, rest in invocations(text)} | set(re.findall(r"`([a-z]+(?:-[a-z]+)*)`", text))
        missing = sorted(set(self.commands) - documented)
        self.assertEqual(missing, [], "add new subcommands to README.md or AGENTS.md")


if __name__ == "__main__":
    unittest.main()
