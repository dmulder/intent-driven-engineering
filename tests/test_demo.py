import importlib.util
from pathlib import Path
import tempfile
import unittest
from support import PROJECT


class DemoTests(unittest.TestCase):
    def test_required_demonstration(self):
        spec = importlib.util.spec_from_file_location("intent_demo", PROJECT / "examples/demo.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            report = module.demonstrate(Path(directory) / "repo")
            self.assertEqual(report["events"], 6)
