"""The vendored Wry patch (src-tauri/vendor/README.md) must be the only Wry in the build."""
from pathlib import Path
import re
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
TAURI = ROOT / "src-tauri"


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

    def test_patch_is_recorded_and_marked_in_source(self):
        patch = (TAURI / "vendor" / "wry-host-input.patch").read_text(encoding="utf-8")
        changed = set(re.findall(r"^\+\+\+ b/wry/(\S+)", patch, re.M))
        self.assertEqual(changed, {"src/lib.rs", "src/webview2/mod.rs"})
        for name in changed:
            source = (TAURI / "vendor" / "wry" / name).read_text(encoding="utf-8")
            self.assertIn("MURMUR PATCH", source)


if __name__ == "__main__":
    unittest.main()
