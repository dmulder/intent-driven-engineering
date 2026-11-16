import unittest
from support import canonical, loads, seal
from intentlib.core import IntentError, digest
from intentlib.schema import SCHEMAS, check, published


class CanonicalTests(unittest.TestCase):
    def test_order_and_hash(self):
        self.assertEqual(canonical({"z": [1, "é"], "a": 2}), canonical({"a": 2, "z": [1, "é"]}))
        self.assertEqual(digest(canonical({"a": 1})), "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862")
        self.assertEqual(seal(seal({"a": 1})), seal({"a": 1}))

    def test_ambiguous_json_rejected(self):
        for raw in (b'{"a":1,"a":2}', b'{"x":1.0}', b'{"x":NaN}'):
            with self.assertRaises(IntentError):
                loads(raw)
        with self.assertRaises(IntentError):
            canonical(1.5)

    def test_schema_required_types_unknown_fields(self):
        self.assertTrue(check({}, SCHEMAS["manifest"]))
        self.assertTrue(check({"schema_version": True, "intents": {}, "bindings": {}, "events": []}, SCHEMAS["manifest"]))
        self.assertTrue(check({"schema_version": 1, "intents": {}, "bindings": {}, "events": [], "surprise": 1}, SCHEMAS["manifest"]))
        self.assertFalse(check({"schema_version": 1, "intents": {}, "bindings": {}, "events": []}, SCHEMAS["manifest"]))

    def test_evaluator_supports_every_published_keyword(self):
        allowed = {"$schema", "type", "properties", "required", "additionalProperties", "items", "enum", "pattern", "minimum", "minLength", "anyOf"}
        def walk(s):
            self.assertTrue(set(s) <= allowed)
            for child in s.get("properties", {}).values(): walk(child)
            for child in s.get("anyOf", []): walk(child)
            for key in ("items", "additionalProperties"):
                if isinstance(s.get(key), dict): walk(s[key])
        for name in SCHEMAS:
            walk(published(name))

    def test_metadata_write_does_not_follow_symlink(self):
        import tempfile
        from pathlib import Path
        from intentlib.core import write_json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "outside").mkdir()
            (root / "link").symlink_to(root / "outside", target_is_directory=True)
            with self.assertRaises(IntentError):
                write_json(root / "link/test.json", {"a": 1})
            self.assertFalse((root / "outside/test.json").exists())
