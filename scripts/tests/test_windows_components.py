"""Windows component builder and the manifest's Windows inputs (no Windows machine needed)."""
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
import unittest.mock
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_engine_imports import fake_pe  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


windows = load("windows_components", SCRIPTS / "components/windows_components.py")
make_manifest = load("make_manifest", SCRIPTS / "components/make-manifest.py")


class WindowsComponentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()  # Windows %TEMP% may be an 8.3 short name

    def engine(self):
        engine = self.root / "murmur-engine"
        ct2 = engine / "_internal" / "ctranslate2"
        ct2.mkdir(parents=True)
        (engine / "murmur-engine.exe").write_bytes(fake_pe(["KERNEL32.dll"]))
        (ct2 / "ctranslate2.dll").write_bytes(fake_pe(["KERNEL32.dll", "libiomp5md.dll"]) + b"cublas64_12.dll\0")
        # The real wheel's shim: KERNEL32 only, but names the cuDNN sub-libraries.
        (ct2 / "cudnn64_9.dll").write_bytes(fake_pe(["KERNEL32.dll"]) + b"cudnn_ops64_9.dll\0")
        return engine

    def test_engine_zip_drops_the_cudnn_shim_and_passes_the_gate(self):
        engine = self.engine()
        with self.assertRaisesRegex(RuntimeError, "cuDNN"):
            windows._gate().check(engine, platform="linux")
        path = windows.build_engine(engine, "1.2.3", self.root / "out", platform="linux")
        self.assertEqual(path.name, "murmur-engine-windows-x86_64-1.2.3.zip")
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(sorted(archive.namelist()),
                             ["_internal/ctranslate2/ctranslate2.dll", "murmur-engine.exe"])

    def test_engine_with_a_real_cudnn_import_still_fails(self):
        engine = self.engine()
        (engine / "_internal/ctranslate2/ctranslate2.dll").write_bytes(fake_pe(["KERNEL32.dll"], ["cudnn64_9.dll"]))
        with self.assertRaisesRegex(RuntimeError, "cudnn64_9.dll"):
            windows.build_engine(engine, "1.2.3", self.root / "out", platform="linux")
        with self.assertRaisesRegex(RuntimeError, "Not a onedir"):
            windows.prune_engine(self.root)

    def wheel(self, cublas=None):
        path = self.root / "nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("nvidia/cublas/bin/cublas64_12.dll", cublas or fake_pe(["KERNEL32.dll", "cublasLt64_12.dll"]))
            archive.writestr("nvidia/cublas/bin/cublasLt64_12.dll", fake_pe(["KERNEL32.dll"]))
            archive.writestr("nvidia/cublas/bin/nvblas64_12.dll", fake_pe(["KERNEL32.dll"]))
            archive.writestr("nvidia/cublas/include/cublas.h", "header")
            archive.writestr("nvidia_cublas_cu12-12.9.2.10.dist-info/License.txt", "NVIDIA EULA")
        lock = self.root / "lock.txt"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lock.write_text(f"nvidia-cublas-cu12==12.9.2.10 --hash=sha256:{digest}\n")
        return path, lock

    def test_cuda_zip_holds_only_cublas_and_the_licence(self):
        wheel, lock = self.wheel()
        path = windows.build_cuda(wheel, lock, "1.2.3", self.root / "out")
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(sorted(archive.namelist()), ["NVIDIA-CUBLAS-LICENSE.txt", "cublas64_12.dll",
                                                          "cublasLt64_12.dll", "cuda-manifest.json"])
            record = json.loads(archive.read("cuda-manifest.json"))
        self.assertEqual(record["source"]["version"], "12.9.2.10")
        self.assertIn("cublasLt64_12.dll", record["files"]["cublas64_12.dll"]["imports"])
        self.assertEqual(list((self.root / "out").iterdir()), [path], "staging removed")

    def test_staging_cleanup_waits_out_a_scanner_holding_a_dll(self):
        # Defender/the indexer briefly hold freshly written DLLs open (WinError 32).
        staging = self.root / "staging"
        staging.mkdir()
        real = windows.shutil.rmtree
        calls = []
        def busy_then_free(path):
            calls.append(path)
            if len(calls) < 3:
                raise PermissionError(32, "being used by another process")
            real(path)
        with unittest.mock.patch.object(windows.shutil, "rmtree", busy_then_free):
            windows._remove_staging(staging, delay=0)
        self.assertEqual(len(calls), 3)
        self.assertFalse(staging.exists())
        # Still held after every attempt: warn and leave it, do not fail the build.
        staging.mkdir()
        with unittest.mock.patch.object(windows.shutil, "rmtree", side_effect=PermissionError(32, "busy")):
            windows._remove_staging(staging, attempts=2, delay=0)
        self.assertTrue(staging.exists())

    def test_cuda_refuses_another_wheel_or_a_cudnn_dependency(self):
        wheel, lock = self.wheel()
        lock.write_text("nvidia-cublas-cu12==12.9.2.10 --hash=sha256:" + "0" * 64 + "\n")
        with self.assertRaisesRegex(RuntimeError, "does not match the SHA-256"):
            windows.build_cuda(wheel, lock, "1.2.3", self.root / "out")
        wheel, lock = self.wheel(fake_pe(["cublasLt64_12.dll"], ["nvrtc64_120_0.dll"]))
        with self.assertRaisesRegex(RuntimeError, "links \\['nvrtc64_120_0.dll'\\]"):
            windows.build_cuda(wheel, lock, "1.2.3", self.root / "out")

    def test_lazy_nvrtc_name_is_recorded_not_fatal(self):
        # Real cublasLt64_12.dll 12.9.2.10: imports KERNEL32 only, names NVRTC for optional JIT.
        wheel, lock = self.wheel(fake_pe(["KERNEL32.dll", "cublasLt64_12.dll"]) + b"nvrtc64_120_0.dll\0")
        path = windows.build_cuda(wheel, lock, "1.2.3", self.root / "out")
        with zipfile.ZipFile(path) as archive:
            record = json.loads(archive.read("cuda-manifest.json"))
        self.assertEqual(record["files"]["cublas64_12.dll"]["lazy_references"], ["nvrtc64_120_0.dll"])

    def test_pinned_locks_are_win_amd64(self):
        version, digest = windows.lock_pin(windows.CUDA_LOCK, "nvidia-cublas-cu12")
        self.assertEqual(version, "12.9.2.10")
        self.assertEqual(len(digest), 64)
        text = windows.ENGINE_LOCK.read_text()
        self.assertIn("ctranslate2-4.6.3-cp312-cp312-win_amd64.whl", text)
        self.assertNotIn("nvidia-", text.replace("# NVIDIA cuBLAS", ""))
        for line in text.splitlines():
            if line and not line.startswith("#"):
                self.assertRegex(line, r"^[\w.-]+==[\w.]+ --hash=sha256:[a-f0-9]{64}$")


class WindowsManifestInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.components = Path(self.tmp.name).resolve()
        for name in ("murmur-engine-linux-x86_64-1.2.3.zip", "murmur-cuda-linux-x86_64-1.2.3.zip",
                     "murmur-engine-windows-x86_64-1.2.3.zip", "murmur-cuda-windows-x86_64-1.2.3.zip"):
            with zipfile.ZipFile(self.components / name, "w") as archive:
                archive.writestr("file", name)
        self.provenance = tomllib.loads((SCRIPTS / "components/provenance.toml").read_text())

    def build(self, inputs):
        now = datetime.datetime(2026, 11, 7, tzinfo=datetime.timezone.utc)
        return make_manifest.build(self.provenance, self.components, "1.2.3", "https://github.com/Zethrus/Murmur/releases/download/v1.2.3/",
                                   1, now, now + datetime.timedelta(days=1),
                                   SCRIPTS.parent / "engine/requirements-linux-release.txt", inputs)

    def test_windows_entries_use_their_own_locks_and_match_the_inputs(self):
        inputs = windows.inputs(self.components, "1.2.3", self.components / windows.INPUTS)
        manifest = self.build(inputs)
        zips = {(c["id"], c["os"]): c for c in manifest["components"] if c["archive"] == "zip"}
        self.assertEqual(set(zips), {("engine", "linux"), ("cuda", "linux"), ("engine", "windows"), ("cuda", "windows")})
        cuda = zips[("cuda", "windows")]
        self.assertEqual(cuda["provenance"]["source_revision"], "nvidia-cublas-cu12==12.9.2.10")
        self.assertEqual(cuda["sha256"], inputs["components"]["cuda"]["sha256"])
        self.assertEqual(zips[("engine", "windows")]["provenance"]["source_sha256"],
                         hashlib.sha256((SCRIPTS.parent / "engine/requirements-windows-release.txt").read_bytes()).hexdigest())
        self.assertEqual(zips[("cuda", "linux")]["provenance"]["source_revision"], "nvidia-cublas-cu12==12.9.3.13")

    def test_windows_entries_skipped_without_inputs_and_required_with_them(self):
        manifest = self.build(None)
        self.assertFalse(any(c["os"] == "windows" for c in manifest["components"]))
        inputs = windows.inputs(self.components, "1.2.3", self.components / windows.INPUTS)
        with zipfile.ZipFile(self.components / "murmur-cuda-windows-x86_64-1.2.3.zip", "w") as archive:
            archive.writestr("file", "rebuilt")
        with self.assertRaisesRegex(SystemExit, "differs from the Windows build"):
            self.build(inputs)
        (self.components / "murmur-cuda-windows-x86_64-1.2.3.zip").unlink()
        with self.assertRaisesRegex(SystemExit, "zip missing"):
            self.build(inputs)
        with self.assertRaisesRegex(SystemExit, "another version"):
            self.build(dict(inputs, version="9.9.9"))


if __name__ == "__main__":
    unittest.main()
