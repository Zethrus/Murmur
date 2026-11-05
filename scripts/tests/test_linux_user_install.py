"""Exercise user-file handling with fixture executables, never build/run Murmur."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("user_install", ROOT / "scripts/linux-user-install.py")
assert SPEC is not None and SPEC.loader is not None
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class UserFiles(unittest.TestCase):
    def setUp(self):
        cache = ROOT / ".cache"
        cache.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="install-test-", dir=cache)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / r"home with 'quotes' $cash %marks `ticks` \\\ slash"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": "", "XDG_DATA_HOME": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.refresh = patch.object(INSTALLER, "refresh")
        self.refresh.start()
        self.addCleanup(self.refresh.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.binary = self.base / "fixture-app"
        self.binary.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@"\n')
        self.binary.chmod(0o755)
        self.sidecar = self.base / "fixture-sidecar"
        self.sidecar.write_text("#!/bin/sh\nexit 0\n")
        self.sidecar.chmod(0o755)

    def install(self):
        INSTALLER.install(self.binary, self.sidecar)

    def test_engine_path_rejects_symlink_before_venv_writes(self):
        engine = self.home / ".local/share/murmur/engine"
        engine.parent.mkdir(parents=True)
        elsewhere = self.base / "unrelated"
        elsewhere.mkdir()
        engine.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(ValueError):
            INSTALLER.engine_path()
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_install_verify_repeat_launcher_and_desktop(self):
        self.install()
        prefix = self.home / ".local"
        app = prefix / "share/murmur"
        self.assertEqual((app / "bin/murmur").read_bytes(), self.binary.read_bytes())
        self.assertEqual((app / "bin/murmur-engine").read_bytes(), self.sidecar.read_bytes())
        result = subprocess.run([str(prefix / "bin/murmur"), "one argument", "$literal"],
                                check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout, "one argument\n$literal\n")
        self.assertEqual((prefix / "bin/murmur").stat().st_mode & 0o777, 0o755)
        for size in INSTALLER.ICONS:
            self.assertTrue((prefix / f"share/icons/hicolor/{size}x{size}/apps/murmur.png").is_file())
        config = self.home / ".config/murmur/config.toml"
        self.assertIn("autostart = true", config.read_text())
        config.write_text('[general]\nautostart = false\n# retained\n')
        models = app / "models"
        models.mkdir()
        (models / "keep").write_text("fixture model marker")
        self.install()
        self.assertIn("# retained", config.read_text())
        self.assertTrue((models / "keep").exists())
        if shutil.which("desktop-file-validate"):
            subprocess.run(["desktop-file-validate", str(prefix / "share/applications/murmur.desktop")], check=True)
            subprocess.run(["desktop-file-validate", str(self.home / ".config/autostart/murmur.desktop")], check=True)

    def test_uninstall_keeps_data_and_purge_removes_it(self):
        self.install()
        app = self.home / ".local/share/murmur"
        (app / "models").mkdir()
        for name in ("models/fixture", "history.sqlite", "murmur.log"):
            (app / name).write_text("fixture data")
        unrelated = self.home / ".local/bin/not-murmur"
        unrelated.write_text("keep")
        INSTALLER.uninstall(False)
        INSTALLER.uninstall(False)
        self.assertFalse((app / "bin").exists())
        self.assertFalse((app / "icons").exists())
        self.assertFalse((self.home / ".local/share/applications/murmur.desktop").exists())
        self.assertFalse((self.home / ".config/autostart/murmur.desktop").exists())
        self.assertEqual(list((self.home / ".local/share/icons/hicolor").glob("*/apps/murmur.png")), [])
        self.assertTrue((app / "models/fixture").exists())
        self.assertTrue((app / "history.sqlite").exists())
        self.assertTrue((self.home / ".config/murmur/config.toml").exists())
        INSTALLER.uninstall(True)
        INSTALLER.uninstall(True)
        self.assertFalse(app.exists())
        self.assertFalse((self.home / ".config/murmur").exists())
        self.assertTrue(unrelated.exists())

    def test_custom_xdg_paths(self):
        config, data = self.base / "xdg-config", self.base / "xdg-data"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config), "XDG_DATA_HOME": str(data)}):
            self.install()
            self.assertTrue((config / "autostart/murmur.desktop").exists())
            self.assertTrue((data / "applications/murmur.desktop").exists())
            (data / "murmur/models").mkdir(parents=True)
            INSTALLER.uninstall(False)
            self.assertTrue((data / "murmur/models").exists())
            INSTALLER.uninstall(True)
            self.assertFalse((config / "murmur").exists())
            self.assertFalse((data / "murmur").exists())

    def test_missing_inputs_fail_before_install(self):
        self.sidecar.unlink()
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse((self.home / ".local").exists())

    def test_symlink_destination_refused_without_touching_target(self):
        target = self.base / "unrelated"
        target.mkdir()
        (target / "keep").write_text("untouched")
        (self.home / ".local").symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.install()
        with self.assertRaises(ValueError):
            INSTALLER.uninstall(True)
        self.assertEqual((target / "keep").read_text(), "untouched")

    def test_relative_xdg_refused(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative"}):
            with self.assertRaises(ValueError):
                self.install()


if __name__ == "__main__":
    unittest.main()
