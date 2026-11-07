"""The cuDNN/NVRTC gate must fail closed on any library-name reference."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("gate", Path(__file__).parents[1] / "check-engine-imports.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class EngineImportGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = Path(self.tmp.name) / "murmur-engine"
        internal = self.engine / "_internal"
        (internal / "ctranslate2.libs").mkdir(parents=True)
        (internal / "ctranslate2.libs/libctranslate2-abc.so.4.6.3").write_bytes(b"\x00cublasCreate_v2\x00libcublas.so.12\x00")
        # onnxruntime option names mention cuDNN but cannot load it.
        (internal / "libonnxruntime.so.1").write_bytes(b"cudnn_conv_algo_search\x00")
        (self.engine / "murmur-engine").write_bytes(b"bootloader")

    def test_cublas_only_engine_passes(self):
        self.assertEqual(gate.check(self.engine, platform="test"), 3)

    def test_library_name_in_any_file_fails(self):
        for name, content in (("libfake.so", b"\x00libcudnn.so.9\x00"),
                              ("libctranslate2-x.so", b"dlopen libcudnn_ops.so.9"),
                              ("_ext.so", b"libnvrtc.so.12"),
                              ("ctranslate2.dll", b"cudnn64_9.dll"),
                              ("loader.pyd", b"nvrtc64_120_0.dll")):
            with self.subTest(name=name):
                bad = self.engine / "_internal" / name
                bad.write_bytes(content)
                with self.assertRaisesRegex(RuntimeError, "references cuDNN/NVRTC"):
                    gate.check(self.engine, platform="test")
                bad.unlink()

    def test_reference_split_across_read_chunks_is_found(self):
        bad = self.engine / "_internal/libbig.so"
        bad.write_bytes(bytes(gate.CHUNK - 4) + b"libcudnn.so.9")
        with self.assertRaises(RuntimeError):
            gate.check(self.engine, platform="test")

    def test_elf_needed_entry_fails_on_linux(self):
        elf = self.engine / "_internal/libneeds.so"
        elf.write_bytes(b"\x7fELF" + bytes(60))
        with patch.object(gate, "_elf_needed", return_value={"libCUDNN.so.9"}):
            with self.assertRaisesRegex(RuntimeError, "libcudnn.so.9"):
                gate.check(self.engine, platform="linux")

    def test_pe_import_fails_on_windows(self):
        dll = self.engine / "_internal/ctranslate2_ext.dll"
        dll.write_bytes(b"MZ" + bytes(62))
        with patch.object(gate, "_pe_imports", return_value={"CUDNN64_9.DLL"}):
            with self.assertRaisesRegex(RuntimeError, "cudnn64_9.dll"):
                gate.check(self.engine, platform="win32")

    def test_site_packages_python_source_is_scanned(self):
        site = Path(self.tmp.name) / "site-packages"
        (site / "ctranslate2").mkdir(parents=True)
        (site / "ctranslate2/__init__.py").write_text("import os\n")
        (site / "ctranslate2/__pycache__").mkdir()
        (site / "ctranslate2/__pycache__/x.cpython-312.pyc").write_bytes(b"libcudnn.so.9")  # stale cache, ignored
        (site / "unrelated").mkdir()
        (site / "unrelated/loader.py").write_text('ctypes.CDLL("libcudnn.so.9")\n')
        self.assertGreater(gate.check(self.engine, platform="test", site_packages=site), 3)
        (site / "ctranslate2/_loader.py").write_text('ctypes.CDLL("libcudnn.so.9")\n')
        with self.assertRaisesRegex(RuntimeError, "packages reference cuDNN/NVRTC"):
            gate.check(self.engine, platform="test", site_packages=site)

    def test_onnxruntime_preload_table_is_benign_only_without_cuda_provider(self):
        site = Path(self.tmp.name) / "site-ort"
        (site / "ctranslate2").mkdir(parents=True)
        (site / "onnxruntime/capi").mkdir(parents=True)
        (site / "onnxruntime/__init__.py").write_text('("nvidia", "cudnn", "lib", "libcudnn.so.9")\n')
        gate.check(self.engine, platform="test", site_packages=site)
        (site / "onnxruntime/capi/libonnxruntime_providers_cuda.so").write_bytes(b"\x7fELF")
        with self.assertRaisesRegex(RuntimeError, "CUDA provider"):
            gate.check(self.engine, platform="test", site_packages=site)

    def test_site_packages_without_ctranslate2_is_rejected(self):
        empty = Path(self.tmp.name) / "site-empty"
        empty.mkdir()
        with self.assertRaisesRegex(RuntimeError, "No ctranslate2 package"):
            gate.check(self.engine, platform="test", site_packages=empty)

    def test_directory_without_ctranslate2_is_rejected(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(RuntimeError, "No CTranslate2"):
            gate.check(empty)
        with self.assertRaisesRegex(RuntimeError, "not found"):
            gate.check(Path(self.tmp.name) / "missing")


if __name__ == "__main__":
    unittest.main()
