"""Shared inference module and the Windows GPU validation's pure parts (no GPU needed)."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import inference_common  # noqa: E402
import linux_components  # noqa: E402

spec = importlib.util.spec_from_file_location("windows_inference", Path(__file__).parents[1] / "test-windows-inference.py")
windows = importlib.util.module_from_spec(spec)
spec.loader.exec_module(windows)
CUDA = r"C:\Users\ci\AppData\Local\Temp\p\Local\Murmur\components\cuda\0.5.0"


class FakeEngine:
    """Answers the engine's JSON-lines protocol from canned results."""
    def __init__(self, device="cuda"):
        self.device, self.calls = device, []
        self.stdin = self

    def write(self, line):
        self.calls.append(json.loads(line))

    def flush(self):
        pass

    def read_line(self, timeout):
        call = self.calls[-1]
        params = call["params"]
        if call["method"] == "transcribe":
            self.compute = params.get("compute_type", "auto")
            result = {"text": "And so my fellow Americans.", "infer_ms": 10.0}
        else:
            result = {"device": self.device, "compute": getattr(self, "compute", "auto"), "cpu_fallback": False}
        return json.dumps({"id": call["id"], "ok": True, "result": result}) + "\n"


class SharedInferenceTests(unittest.TestCase):
    def test_matrix_and_fixture(self):
        self.assertEqual(len(inference_common.MATRIX), 8)
        pcm = inference_common.fixture_pcm(Path(__file__).parent / "jfk-3s.wav")
        self.assertGreater(len(pcm), 1000)

    def test_runs_cover_warm_runs_status_and_matrix(self):
        engine = FakeEngine()
        request = inference_common.requester(engine, engine.read_line, "cuda")
        result = inference_common.transcription_runs(request, "distil-small.en", "C:/m", "cuda", "pcm", 10,
                                                     inference_common.MATRIX, 0)
        self.assertEqual(len(result["warm_runs_ms"]), 10)
        self.assertEqual(len(result["matrix"]), 8)
        self.assertEqual(len({c["transcript_sha256"] for c in result["matrix"]}), 1)
        inference_common.require_device(result, "cuda", "Forced CUDA")
        with self.assertRaisesRegex(RuntimeError, "did not stay on cpu"):
            inference_common.require_device(result, "cpu", "CPU")

    def test_timeouts_and_engine_errors_are_reported(self):
        engine = FakeEngine()
        def timeout(_):
            raise TimeoutError
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            inference_common.requester(engine, timeout, "cuda")("status")
        with self.assertRaisesRegex(RuntimeError, "engine exited: boom"):
            inference_common.requester(engine, lambda _: "", "cuda", lambda: "boom")("status")
        failing = lambda _: json.dumps({"ok": False, "error": "CUDA failed"})  # noqa: E731
        with self.assertRaisesRegex(RuntimeError, "CUDA failed"):
            inference_common.requester(engine, failing, "cuda")("status")

    def test_threaded_reader_reads_lines_and_times_out(self):
        read = inference_common.threaded_reader(io.StringIO("one\ntwo\n"))
        self.assertEqual(read(1), "one\n")
        self.assertEqual(read(1), "two\n")
        self.assertEqual(read(1), "")
        r, w = os.pipe()
        with os.fdopen(r) as stream, os.fdopen(w, "w"):
            with self.assertRaises(TimeoutError):
                inference_common.threaded_reader(stream)(0.05)


class WindowsValidationTests(unittest.TestCase):
    def modules(self, *extra):
        return [rf"{CUDA}\cublas64_12.dll", rf"{CUDA}\cublasLt64_12.dll", r"C:\Windows\System32\nvcuda.dll",
                r"C:\Windows\System32\KERNEL32.DLL", *extra]

    def test_cublas_from_the_add_on_and_driver_from_the_host_pass(self):
        windows.check_origin(self.modules(), CUDA)
        # Case and separators differ between Get-Process output and Python paths.
        windows.check_origin([m.upper() for m in self.modules()], CUDA.replace("\\", "/"))

    def test_cudnn_nvrtc_or_foreign_cublas_fails(self):
        for module, message in ((r"C:\x\components\engine\1\_internal\ctranslate2\cudnn64_9.dll", "cuBLAS only"),
                                (r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9\bin\nvrtc64_120_0.dll", "cuBLAS only")):
            with self.subTest(module=module), self.assertRaisesRegex(RuntimeError, message):
                windows.check_origin(self.modules(module), CUDA)
        modules = self.modules()
        modules[0] = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9\bin\cublas64_12.dll"
        with self.assertRaisesRegex(RuntimeError, "cublas64_12.dll did not load solely"):
            windows.check_origin(modules, CUDA)
        with self.assertRaisesRegex(RuntimeError, "Host driver"):
            windows.check_origin(self.modules()[:2], CUDA)
        # A component folder whose name merely starts like the add-on's is not inside it.
        with self.assertRaisesRegex(RuntimeError, "solely"):
            windows.check_origin([m.replace(r"\0.5.0", r"\0.5.0-evil") for m in self.modules()], CUDA)

    def test_module_listing_parser(self):
        output = "C:\\x\\murmur-engine.exe\r\nC:\\x\\a.DLL\r\n\r\nC:\\x\\a.DLL\r\nC:\\y\\b.dll\r\n"
        self.assertEqual(windows.parse_modules(output), ["C:\\x\\a.DLL", "C:\\y\\b.dll"])

    def test_engine_environment_has_no_host_cuda(self):
        host = {"PATH": ";".join([r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9\bin",
                                         r"C:\Windows\System32", r"C:\tools\cudnn\bin"]),
                "CUDA_PATH": "x", "CUDA_PATH_V12_9": "x", "MURMUR_CUDA_DIR": "stale", "CUDA_VISIBLE_DEVICES": "1"}
        with patch.dict(os.environ, host, clear=True):
            env = windows.clean_environment(Path("/tmp/home"), CUDA, pathsep=";")
            hidden = windows.clean_environment(Path("/tmp/home"), None, hidden=True, pathsep=";")
        self.assertEqual(env["PATH"], r"C:\Windows\System32")
        self.assertEqual(env["MURMUR_CUDA_DIR"], CUDA)
        self.assertNotIn("CUDA_PATH", env)
        self.assertNotIn("CUDA_PATH_V12_9", env)
        self.assertNotIn("CUDA_VISIBLE_DEVICES", env)
        self.assertEqual(hidden["CUDA_VISIBLE_DEVICES"], "-1")
        self.assertNotIn("MURMUR_CUDA_DIR", hidden)

    def test_evidence_covers_exactly_the_assets_check_release_ties_it_to(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("Murmur_1.2.3_x64-setup.exe", "murmur-windows-x86_64.zip", "murmur-engine-windows-x86_64-1.2.3.zip",
                         "murmur-cuda-windows-x86_64-1.2.3.zip", "components-manifest.json",
                         "murmur-engine-linux-x86_64-1.2.3.zip", "components-manifest.json.sig"):
                (root / name).write_bytes(name.encode())
            spec = importlib.util.spec_from_file_location("release", Path(__file__).parents[1] / "check-release.py")
            release = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(release)
            self.assertEqual(set(windows.evidence_assets(root, root)),
                             release.windows_assets("1.2.3") | {release.MANIFEST})

    def test_component_environment_on_windows(self):
        env = linux_components.environment(Path("/p"), Path("/c"), base={}, windows=True)
        self.assertEqual(linux_components.data_root(env), Path("/p/Local/Murmur"))
        self.assertNotIn("XDG_DATA_HOME", env)
        linux = linux_components.environment(Path("/p"), Path("/c"), base={})
        self.assertEqual(linux_components.data_root(linux), Path("/p/data/murmur"))


if __name__ == "__main__":
    unittest.main()
