"""Source-tree resource lookup used by the installable Python package."""

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "archify" / "python"))

from archify_runtime.resources import get_resource_root


class NativeInstallationTests(unittest.TestCase):
    def test_checkout_uses_canonical_skill_resources(self):
        resource_root = get_resource_root()
        self.assertEqual(resource_root, ROOT / "archify")
        self.assertTrue((resource_root / "schemas" / "common.schema.json").is_file())
        self.assertTrue((resource_root / "assets" / "template.html").is_file())
        self.assertTrue((resource_root / "LICENSE").is_file())
        self.assertTrue((resource_root / "THIRD_PARTY_NOTICES.md").is_file())


if __name__ == "__main__":
    unittest.main()
