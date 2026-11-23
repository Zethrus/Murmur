"""The vendored Wry patch (src-tauri/vendor/README.md) must be the only Wry in the build."""
from pathlib import Path
import difflib
import hashlib
import importlib.util
import io
import re
import tarfile
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
TAURI = ROOT / "src-tauri"
spec = importlib.util.spec_from_file_location("verify_wry", ROOT / "scripts/verify-vendored-wry.py")
verify_wry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_wry)


class VendoredWryTests(unittest.TestCase):
    def test_exactly_one_wry_and_it_is_the_vendored_copy(self):
        lock = tomllib.loads((TAURI / "Cargo.lock").read_text(encoding="utf-8"))
        wry = [package for package in lock["package"] if package["name"] == "wry"]
        # A second Wry (e.g. after a Tauri upgrade) would receive Murmur's setter while the
        # runtime used the other copy: the overlay option would silently disappear.
        self.assertEqual(len(wry), 1, wry)
        self.assertNotIn("source", wry[0], "wry must resolve to the [patch.crates-io] path")
        vendored = tomllib.loads((TAURI / "vendor" / "wry" / "Cargo.toml").read_text(encoding="utf-8"))
        self.assertEqual(wry[0]["version"], vendored["package"]["version"])

    def test_manifest_pins_the_vendored_version_and_patches_it(self):
        manifest = tomllib.loads((TAURI / "Cargo.toml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["patch"]["crates-io"]["wry"], {"path": "vendor/wry"})
        direct = manifest["target"]["cfg(windows)"]["dependencies"]["wry"]
        vendored = tomllib.loads((TAURI / "vendor" / "wry" / "Cargo.toml").read_text(encoding="utf-8"))
        self.assertEqual(direct["version"], "=" + vendored["package"]["version"])
        self.assertIs(direct["default-features"], False)
        self.assertEqual(vendored["package"]["version"], verify_wry.VERSION)

    def test_patch_is_recorded_and_marked_in_source(self):
        patch = (TAURI / "vendor" / "wry-host-input.patch").read_text(encoding="utf-8")
        changed = set(re.findall(r"^\+\+\+ b/wry/(\S+)", patch, re.M))
        self.assertEqual(changed, {"src/lib.rs", "src/webview2/mod.rs"})
        for name in changed:
            source = (TAURI / "vendor" / "wry" / name).read_text(encoding="utf-8")
            self.assertIn("MURMUR PATCH", source)


class VendorIntegrityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.vendor = self.root / "wry"
        self.vendor.mkdir()
        (self.vendor / "src").mkdir()
        (self.vendor / "src/lib.rs").write_bytes(b"patched\n")
        (self.vendor / "Cargo.toml").write_bytes(b"unchanged\n")
        self.patch = self.root / "wry.patch"
        self.patch.write_text("".join(difflib.unified_diff(
            ["original\n"], ["patched\n"], fromfile="a/wry/src/lib.rs", tofile="b/wry/src/lib.rs"
        )), encoding="utf-8", newline="\n")
        self.data = self.archive({"src/lib.rs": b"original\n", "Cargo.toml": b"unchanged\n",
                                  ".cargo_vcs_info.json": b"metadata"})

    @staticmethod
    def archive(files):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            for name, content in files.items():
                entry = tarfile.TarInfo(f"wry-test/{name}")
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
        return data.getvalue()

    def verify(self, data=None, **kwargs):
        data = self.data if data is None else data
        return verify_wry.verify(data, self.vendor, self.patch,
                                 expected_sha=kwargs.get("sha", hashlib.sha256(data).hexdigest()),
                                 archive_root="wry-test")

    def test_archive_plus_patch_matches_without_modifying_checkout(self):
        before = verify_wry.tree_files(self.vendor)
        self.assertEqual(self.verify(), 2)
        self.assertEqual(verify_wry.tree_files(self.vendor), before)

    def test_unrecorded_change_in_any_file_fails(self):
        for name in ["src/lib.rs", "Cargo.toml"]:
            path = self.vendor / name
            original = path.read_bytes()
            path.write_bytes(original + b"unexpected\n")
            with self.assertRaisesRegex(ValueError, "changed="):
                self.verify()
            path.write_bytes(original)

    def test_missing_extra_and_wrong_hash_fail(self):
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.verify(sha="0" * 64)
        (self.vendor / "extra.rs").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "extra=.*extra.rs"):
            self.verify()
        (self.vendor / "extra.rs").unlink()
        (self.vendor / "Cargo.toml").unlink()
        with self.assertRaisesRegex(ValueError, "missing=.*Cargo.toml"):
            self.verify()

    def test_patch_must_apply_to_the_pinned_archive(self):
        self.patch.write_text(self.patch.read_text().replace("-original", "-other"), encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValueError, "patch does not apply"):
            self.verify()

    def test_archive_parent_traversal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unexpected archive path"):
            self.verify(self.archive({"../outside": b"unsafe"}))


if __name__ == "__main__":
    unittest.main()
