"""Windows draft handoff is an exact, tamper-evident local asset set."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ci_release_stage", ROOT / "scripts" / "ci-release-stage.py")
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)
SHA = "a" * 40
VERSION = "1.2.3"


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / "tool"
        self.source = self.root / "artifacts"
        for name, path in stage.asset_paths(self.source, VERSION).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((name * 2).encode())

    def make_stage(self):
        return stage.write_stage(self.cache, "101", "2", SHA, VERSION, self.source)

    def test_exact_stage_verifies_current_run_attempt_and_sha(self):
        dest = self.make_stage()
        self.assertEqual(stage.verify_stage(self.cache, "101", "2", SHA, VERSION), dest)
        self.assertEqual(len(list(dest.iterdir())), 5)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            stage.verify_stage(self.cache, "101", "2", "b" * 40, VERSION)
        with self.assertRaises(FileNotFoundError):
            stage.verify_stage(self.cache, "101", "1", SHA, VERSION)

    def test_tamper_missing_and_unexpected_assets_fail_closed(self):
        dest = self.make_stage()
        target = dest / "murmur-windows-x86_64.zip"
        target.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "hash or size mismatch"):
            stage.verify_stage(self.cache, "101", "2", SHA, VERSION)
        self.make_stage()
        target.unlink()
        with self.assertRaisesRegex(ValueError, "missing or unexpected"):
            stage.verify_stage(self.cache, "101", "2", SHA, VERSION)
        self.make_stage()
        (dest / "extra.exe").write_bytes(b"unexpected")
        with self.assertRaisesRegex(ValueError, "missing or unexpected"):
            stage.verify_stage(self.cache, "101", "2", SHA, VERSION)

    def test_partial_stage_has_no_ready_manifest(self):
        stage.asset_paths(self.source, VERSION)["murmur-windows-x86_64.zip"].unlink()
        with self.assertRaisesRegex(ValueError, "Missing or redirected"):
            self.make_stage()
        self.assertFalse((stage.stage_path(self.cache, "101", "2") / "stage.json").exists())

    @unittest.skipIf(sys.platform == "win32", "Creating a symlink needs Windows developer privileges")
    def test_redirected_stage_is_rejected_before_cleanup(self):
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "keep"
        marker.write_text("safe")
        redirect = self.cache / "murmur-release-stage" / "101-2"
        redirect.parent.mkdir(parents=True)
        redirect.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink or junction"):
            self.make_stage()
        self.assertEqual(marker.read_text(), "safe")


if __name__ == "__main__":
    unittest.main()
