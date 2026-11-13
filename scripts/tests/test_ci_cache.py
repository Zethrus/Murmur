"""Cache path and workflow guards for CI builds."""
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ci_cache", ROOT / "scripts" / "ci-cache.py")
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.workspace = base / "work" / "Murmur"
        self.workspace.mkdir(parents=True)
        self.tool_cache = base / "tool"
        self.tool_cache.mkdir()
        self.env = {
            "GITHUB_WORKSPACE": str(self.workspace),
            "RUNNER_TOOL_CACHE": str(self.tool_cache),
            "RUNNER_ENVIRONMENT": "self-hosted",
            "GITHUB_REPOSITORY": "Zethrus/Murmur",
            "RUNNER_NAME": "edison",
            "RUNNER_OS": "Windows",
            "RUNNER_ARCH": "X64",
            "GITHUB_ENV": str(base / "env"),
            "GITHUB_PATH": str(base / "path"),
        }

    def test_self_hosted_paths_scoped_and_outside_checkout(self):
        paths = cache.cache_paths(self.env)
        self.assertNotIn(self.workspace, paths["target"].parents)
        self.assertIn("rust-1.100.0", str(paths["target"]))
        for key in ("GITHUB_REPOSITORY", "RUNNER_NAME", "RUNNER_OS", "RUNNER_ARCH"):
            altered = dict(self.env, **{key: self.env[key] + "-other"})
            self.assertNotEqual(paths["target"], cache.cache_paths(altered)["target"])
        cache.prepare(self.env)
        output = Path(self.env["GITHUB_ENV"]).read_text()
        for key in ("CARGO_TARGET_DIR", "PIP_CACHE_DIR", "NPM_CONFIG_CACHE", "CARGO_HOME", "RUSTUP_HOME"):
            self.assertIn(key + "=", output)
        self.assertIn("TMP=", output)
        self.assertIn("TEMP=", output)
        self.assertTrue(paths["target"].is_dir())

    def test_hosted_uses_checkout_and_no_runner_local_cache(self):
        hosted = dict(self.env, RUNNER_ENVIRONMENT="github-hosted")
        paths = cache.cache_paths(hosted)
        self.assertEqual(paths["target"], self.workspace / "src-tauri" / "target")
        cache.prepare(hosted)
        self.assertFalse((self.tool_cache / "murmur-build-cache").exists())

    def test_pruning_is_bounded_to_own_namespace(self):
        paths = cache.cache_paths(self.env)
        paths["target"].mkdir(parents=True)
        (paths["target"] / "oversized").write_bytes(b"123456")
        sibling = paths["root"].parent / "rust-1.99.1"
        sibling.mkdir(parents=True)
        old = sibling / ".last-used"
        old.touch()
        past = time.time() - 20 * 86400
        os.utime(old, (past, past))
        unrelated = self.tool_cache / "other-repo"
        unrelated.mkdir()
        (unrelated / "keep").touch()
        with mock.patch.object(cache, "MAX_TARGET_BYTES", 5):
            cache.prepare(self.env)
        self.assertFalse((paths["target"] / "oversized").exists())
        self.assertFalse(sibling.exists())
        self.assertTrue((unrelated / "keep").exists())

    def test_reject_cache_inside_checkout_and_symlink_target(self):
        bad = dict(self.env, RUNNER_TOOL_CACHE=str(self.workspace / ".cache"))
        with self.assertRaisesRegex(ValueError, "outside"):
            cache.cache_paths(bad)
        paths = cache.cache_paths(self.env)
        paths["root"].mkdir(parents=True)
        outside = self.tool_cache / "unrelated"
        outside.mkdir()
        (outside / "keep").touch()
        try:
            paths["target"].symlink_to(outside, target_is_directory=True)
        except OSError:
            # Standard Windows runner accounts may lack symlink creation rights.
            return
        with self.assertRaisesRegex(ValueError, "symlink"):
            cache.prepare(self.env)
        self.assertTrue((outside / "keep").exists())

    def test_cargo_download_prune_keeps_rustup_shim(self):
        paths = cache.cache_paths(self.env)
        bin_dir = paths["cargo"] / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "rustup").write_bytes(b"shim")
        registry = paths["cargo"] / "registry"
        registry.mkdir()
        (registry / "old-crate").write_bytes(b"123456")
        with mock.patch.object(cache, "MAX_DOWNLOAD_BYTES", 5):
            cache.prepare(self.env)
        self.assertEqual((bin_dir / "rustup").read_bytes(), b"shim")
        self.assertFalse((registry / "old-crate").exists())

    def test_total_namespace_limit_prunes_downloads_before_target(self):
        paths = cache.cache_paths(self.env)
        paths["target"].mkdir(parents=True)
        (paths["target"] / "compiled").write_bytes(b"1234")
        paths["pip"].mkdir(parents=True)
        (paths["pip"] / "wheel").write_bytes(b"1234")
        with mock.patch.object(cache, "MAX_NAMESPACE_BYTES", 5):
            cache.prepare(self.env)
        self.assertTrue((paths["target"] / "compiled").exists())
        self.assertFalse((paths["pip"] / "wheel").exists())

    def test_resource_snapshots_write_summary(self):
        self.env["RUNNER_TEMP"] = self.tmp.name
        self.env["GITHUB_RUN_ID"] = "123"
        self.env["GITHUB_STEP_SUMMARY"] = str(Path(self.tmp.name) / "summary")
        cache.prepare(self.env)
        cache.snapshot(self.env, "start")
        cache.snapshot(self.env, "finish")
        summary = Path(self.env["GITHUB_STEP_SUMMARY"]).read_text()
        self.assertIn("Build cache evidence (start)", summary)
        self.assertIn("Build cache evidence (finish)", summary)
        self.assertIn("available RAM:", summary)


class WorkflowTests(unittest.TestCase):
    def test_pinned_toolchain_cache_routing_and_native_audit(self):
        for platform in ("linux", "windows"):
            text = (ROOT / ".github" / "workflows" / f"{platform}-build.yml").read_text()
            self.assertIn("dtolnay/rust-toolchain@1.100.0", text)
            self.assertNotIn("dtolnay/rust-toolchain@stable", text)
            self.assertIn("scripts/ci-cache.py prepare", text)
            self.assertIn("if: runner.environment != 'self-hosted'\n        with:\n          workspaces: src-tauri -> target", text)
            self.assertIn("runner.environment != 'self-hosted' && 'npm'", text)
            self.assertIn("cancel-in-progress: false", text)
            self.assertIn("scripts/ci-cache.py start", text)
            self.assertIn("scripts/ci-cache.py finish", text)
        windows = (ROOT / ".github" / "workflows" / "windows-build.yml").read_text()
        self.assertNotIn("src-tauri/target/x86_64-pc-windows-msvc/release", windows)
        self.assertIn("--artifact (Join-Path $Release 'murmur.exe')", windows)
        self.assertIn("-SkipComponents -Release $Release", windows)
        self.assertIn("(Join-Path $Release 'murmur.exe') .cache/artifacts/windows/murmur-windows-x86_64.zip", windows)


if __name__ == "__main__":
    unittest.main()
