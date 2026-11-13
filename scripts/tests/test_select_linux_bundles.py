"""The persistent Cargo target may contain bundles from older releases."""
from pathlib import Path
import subprocess
import tempfile
import unittest


SELECTOR = Path(__file__).resolve().parents[1] / "select-linux-bundles.sh"


class SelectLinuxBundlesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.release = Path(self.temporary.name) / "release"
        (self.release / "bundle/appimage").mkdir(parents=True)
        (self.release / "bundle/deb").mkdir()

    def bundle(self, version, extension):
        directory = "appimage" if extension == "AppImage" else "deb"
        path = self.release / "bundle" / directory / f"Murmur_{version}_amd64.{extension}"
        path.write_bytes(version.encode())
        return path

    def select(self):
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c",
             'source "$1"; select_linux_bundles "$2" 0.5.0; '
             'printf "%s\\n%s\\n" "$LINUX_APPIMAGE" "$LINUX_DEB"',
             "bash", str(SELECTOR), str(self.release)],
            capture_output=True, text=True,
        )

    def test_selects_current_version_with_older_bundles_present(self):
        old = [self.bundle("0.4.7", extension) for extension in ("AppImage", "deb")]
        current = [self.bundle("0.5.0", extension) for extension in ("AppImage", "deb")]
        result = self.select()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(path) for path in current])
        self.assertTrue(all(path.exists() for path in old))

    def test_missing_current_bundle_fails_even_when_older_one_exists(self):
        for missing in ("AppImage", "deb"):
            with self.subTest(missing=missing):
                for extension in ("AppImage", "deb"):
                    self.bundle("0.4.7", extension)
                    if extension != missing:
                        self.bundle("0.5.0", extension)
                result = self.select()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"Murmur_0.5.0_amd64.{missing}", result.stderr)
                self.assertEqual(result.stdout, "")
                for extension in ("AppImage", "deb"):
                    (self.release / "bundle" / ("appimage" if extension == "AppImage" else "deb") /
                     f"Murmur_0.5.0_amd64.{extension}").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
