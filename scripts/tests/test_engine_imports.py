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

    def test_wheel_metadata_listing_a_pruned_dll_is_ignored(self):
        # The Windows CTranslate2 wheel's RECORD names cudnn64_9.dll even after the shim is pruned.
        info = self.engine / "_internal" / "ctranslate2-4.6.3.dist-info"
        info.mkdir()
        (info / "RECORD").write_text("ctranslate2/cudnn64_9.dll,sha256=abc,266240\n")
        self.assertEqual(gate.check(self.engine, platform="test"), 4)
        # A real file of that name next to the metadata still fails.
        (self.engine / "_internal" / "cudnn64_9.dll").write_bytes(b"MZ")
        with self.assertRaisesRegex(RuntimeError, "references cuDNN/NVRTC"):
            gate.check(self.engine, platform="test")

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


def fake_pe(imports=(), delay_imports=(), pe32=False, directories_in_header=16, rva_count=16,
            name_rva=None):
    """Minimal PE image with an import and a delay-import table in one section.

    `directories_in_header` sizes the optional header, `rva_count` is NumberOfRvaAndSizes, and
    `name_rva` overrides every import name RVA (for unmapped-name tests)."""
    import struct
    optional_size = (96 if pe32 else 112) + directories_in_header * 8
    image = bytearray(0x400)
    image[0:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 0x40)
    image[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", image, 0x44, 0x14C if pe32 else 0x8664, 1, 0, 0, 0, optional_size, 0x22)
    optional = 0x58
    struct.pack_into("<H", image, optional, 0x10B if pe32 else 0x20B)
    directories = optional + (96 if pe32 else 112)
    struct.pack_into("<I", image, directories - 4, rva_count)
    section = optional + optional_size
    image[section:section + 8] = b".rdata\0\0"
    struct.pack_into("<IIII", image, section + 8, 0x1000, 0x1000, 0x200, 0x200)  # vsize, rva, raw size, raw ptr
    rva = lambda offset: 0x1000 + offset - 0x200  # noqa: E731 - file offset in the section -> RVA
    names = 0x300
    descriptor = 0x200
    struct.pack_into("<II", image, directories + 8, rva(descriptor), 20 * (len(imports) + 1))
    for name in imports:
        image[names:names + len(name)] = name.encode()
        struct.pack_into("<IIIII", image, descriptor, 0, 0, 0, name_rva or rva(names), 0)
        descriptor += 20
        names += len(name) + 1
    delay = 0x280
    if directories_in_header > 13:
        struct.pack_into("<II", image, directories + 8 * 13, rva(delay), 32 * (len(delay_imports) + 1))
    for name in delay_imports:
        image[names:names + len(name)] = name.encode()
        struct.pack_into("<II", image, delay, 1, rva(names))
        delay += 32
        names += len(name) + 1
    return bytes(image)


class PortableExecutableTests(unittest.TestCase):
    def test_import_and_delay_import_tables_are_read(self):
        for pe32 in (False, True):
            with self.subTest(pe32=pe32):
                image = fake_pe(["KERNEL32.dll", "cublas64_12.dll"], ["cublasLt64_12.dll"], pe32=pe32)
                self.assertEqual(gate.pe_imports_builtin(image),
                                 {"KERNEL32.dll", "cublas64_12.dll", "cublasLt64_12.dll"})

    def test_image_without_import_directories_gives_nothing(self):
        for pe32 in (False, True):
            with self.subTest(pe32=pe32):
                # No import or delay-import directory at all (RVA 0).
                self.assertEqual(gate.pe_imports_builtin(fake_pe(pe32=pe32, rva_count=0)), set())
                self.assertEqual(gate.pe_imports_builtin(fake_pe(pe32=pe32, directories_in_header=0,
                                                                 rva_count=0)), set())

    def test_short_optional_header_and_rva_count_bound_the_directories(self):
        # Only two directories in the optional header: the import table is read, and the delay
        # directory (index 13) is never looked up beyond the header (it would be section data).
        image = fake_pe(["KERNEL32.dll"], pe32=False, directories_in_header=2)
        self.assertEqual(gate.pe_imports_builtin(image), {"KERNEL32.dll"})
        # Full header but NumberOfRvaAndSizes = 2: the delay directory is not declared.
        image = fake_pe(["KERNEL32.dll"], ["cudnn64_9.dll"], rva_count=2)
        self.assertEqual(gate.pe_imports_builtin(image), {"KERNEL32.dll"})
        # Header too short to hold NumberOfRvaAndSizes itself.
        import struct
        image = bytearray(fake_pe(["KERNEL32.dll"]))
        struct.pack_into("<H", image, 0x40 + 20, 16)
        self.assertEqual(gate.pe_imports_builtin(bytes(image)), set())

    def test_unmapped_name_and_truncated_image_do_not_raise(self):
        # A name RVA outside every section used to reach _c_string as None (TypeError).
        image = fake_pe(["cublas64_12.dll"], name_rva=0x9000)
        self.assertEqual(gate.pe_imports_builtin(image), set())
        # Truncated inside the section table: whatever was readable, no struct.error.
        whole = fake_pe(["KERNEL32.dll"])
        for length in (0x44, 0x5A, 0x160, 0x1F0, 0x210):
            with self.subTest(length=length):
                self.assertIsInstance(gate.pe_imports_builtin(whole[:length]), set)
        # The gate on a file holding such an image reports normally (built-in parser only).
        import sys
        with tempfile.TemporaryDirectory() as temporary, patch.dict(sys.modules, {"pefile": None}):
            path = Path(temporary) / "broken.dll"
            path.write_bytes(image)
            self.assertEqual(gate._pe_imports(path), set())

    def test_not_pe_gives_nothing(self):
        self.assertEqual(gate.pe_imports_builtin(b"\x7fELF" + bytes(100)), set())
        self.assertEqual(gate.pe_imports_builtin(b"MZ" + bytes(100)), set())

    def test_windows_engine_with_cudnn_import_fails_on_any_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = Path(temporary) / "murmur-engine"
            internal = engine / "_internal" / "ctranslate2"
            internal.mkdir(parents=True)
            (internal / "ctranslate2.dll").write_bytes(fake_pe(["cublas64_12.dll", "KERNEL32.dll"]))
            (engine / "murmur-engine.exe").write_bytes(fake_pe(["KERNEL32.dll"]))
            self.assertEqual(gate.check(engine, platform="linux"), 2)
            (internal / "ctranslate2.dll").write_bytes(fake_pe(["cublas64_12.dll"], ["cudnn64_9.dll"]))
            with self.assertRaisesRegex(RuntimeError, "cudnn64_9.dll"):
                gate.check(engine, platform="linux")

    def test_import_names_reach_the_gate_even_if_the_string_scan_misses_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ctranslate2.dll"
            path.write_bytes(fake_pe(["KERNEL32.dll"]))
            with patch.object(gate, "_pe_imports", return_value={"CUDNN64_9.DLL"}) as imports:
                self.assertEqual(gate.references(path, platform="win32"), {"cudnn64_9.dll"})
            imports.assert_called_once_with(path, require_pefile=True)

    def test_windows_requires_pefile_and_merges_its_tables(self):
        import sys
        import types
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "a.dll"
            path.write_bytes(fake_pe(["KERNEL32.dll"]))
            with patch.dict(sys.modules, {"pefile": None}):
                with self.assertRaisesRegex(RuntimeError, "pefile is required"):
                    gate._pe_imports(path, require_pefile=True)
                self.assertEqual(gate._pe_imports(path), {"KERNEL32.dll"})
            entry = types.SimpleNamespace(dll=b"nvrtc64_120_0.dll")
            fake = types.ModuleType("pefile")
            fake.PEFormatError = ValueError
            fake.DIRECTORY_ENTRY = {"IMAGE_DIRECTORY_ENTRY_IMPORT": 1, "IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT": 13}
            fake.PE = lambda *a, **k: types.SimpleNamespace(parse_data_directories=lambda **k: None,
                                                             DIRECTORY_ENTRY_DELAY_IMPORT=[entry])
            with patch.dict(sys.modules, {"pefile": fake}):
                self.assertEqual(gate._pe_imports(path, require_pefile=True), {"KERNEL32.dll", "nvrtc64_120_0.dll"})
