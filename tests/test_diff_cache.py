import difflib
import unittest

from support import PROJECT  # noqa: F401  (puts tools/ on sys.path)
from intentlib.core import intervals, map_preserved_range, opcodes


class DiffCacheTests(unittest.TestCase):
    def test_one_file_pair_is_diffed_once_and_matches_difflib(self):
        opcodes.cache_clear()
        old = b"alpha\nbeta\ngamma\n" * 40
        new = old.replace(b"beta", b"BETA", 3)
        reference = difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
        self.assertEqual(list(opcodes(old, new)), reference)
        before, after = ("100644", old), ("100644", new)
        for _ in range(5):
            intervals(before, after)
            map_preserved_range(len(old) - 6, len(old), before, after)
        self.assertEqual(opcodes.cache_info().misses, 1)

    def test_distinct_pairs_do_not_share_results(self):
        opcodes.cache_clear()
        self.assertNotEqual(opcodes(b"abc", b"abd"), opcodes(b"abc", b"xyz"))
        self.assertEqual(opcodes.cache_info().misses, 2)

    def test_validation_revisits_more_than_sixteen_pairs_without_rediffing(self):
        opcodes.cache_clear()
        pairs = [(bytes([65 + i]) + b"same old", bytes([65 + i]) + b"same new") for i in range(24)]
        for old, new in pairs:
            intervals(("100644", old), ("100644", new))
        for old, new in pairs:
            self.assertEqual(map_preserved_range(0, 1, ("100644", old), ("100644", new)), (0, 1))
        self.assertEqual(opcodes.cache_info().misses, len(pairs))


if __name__ == "__main__":
    unittest.main()
