import importlib.util
import hashlib
import io
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("release", Path(__file__).parents[1] / "check-release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("Murmur_1.2.3_amd64.deb", "Murmur_1.2.3_amd64.AppImage", "murmur-companion-v1.2.3.fap",
                     "murmur-engine-linux-x86_64-1.2.3.zip", "murmur-cuda-linux-x86_64-1.2.3.zip"):
            (self.root / name).write_bytes(name.encode() if name.endswith(".zip") else b"artifact")
        (self.root / "components-manifest.json.sig").write_text("signature")
        (self.root / "murmur-release.asc").write_text("certificate")
        self.manifest()
        self.archive()
        self.evidence()

    def manifest(self, url_base="https://github.com/Zethrus/Murmur/releases/download/v1.2.3/", mutate=None):
        def zip_entry(id, name):
            data = (self.root / name).read_bytes()
            return {"id": id, "os": "linux", "archive": "zip", "urls": [url_base + name],
                    "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        manifest = {"schema_version": 1, "channel": "stable", "components": [
            zip_entry("engine", "murmur-engine-linux-x86_64-1.2.3.zip"), zip_entry("cuda", "murmur-cuda-linux-x86_64-1.2.3.zip"),
            {"id": "speech-tiny.en", "os": "any", "archive": "none",
             "files": [{"name": "model.bin", "urls": ["https://huggingface.co/x/resolve/0123/model.bin"]}]}]}
        if mutate:
            mutate(manifest)
        (self.root / "components-manifest.json").write_text(json.dumps(manifest))

    def evidence(self):
        excluded = {"murmur-companion-v1.2.3.fap", "GPU-VALIDATION.json", "SHA256SUMS", "components-manifest.json.sig", "murmur-release.asc"}
        linux = [p for p in self.root.iterdir() if p.name not in excluded
                 and "windows" not in p.name and not p.name.endswith("_x64-setup.exe")]
        assets = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in linux}
        cuda = "/tmp/data/murmur/components/cuda/1.2.3"
        (self.root / "GPU-VALIDATION.json").write_text(json.dumps({"assets": assets,
            "gpu": {"status": {"device": "cuda", "cpu_fallback": False}, "warm_runs_ms": [1] * 10,
                    "libraries": [f"{cuda}/libcublas.so.12", "/usr/lib/libcuda.so.1"]},
            "auto": {"status": {"device": "cuda", "cpu_fallback": False}},
            "driver": "test GPU, test driver", "origin_validated": True,
            "runtime_probe": {name: f"{cuda}/{name}" for name in ("libcublas.so.12", "libcublasLt.so.12")}}))

    def archive(self, mode=0o755, data=b"artifact", extra=()):
        with tarfile.open(self.root / "murmur-linux-x86_64.tar.gz", "w:gz") as archive:
            for name, contents in (("murmur", data), *extra):
                member = tarfile.TarInfo(name)
                member.mode, member.size = mode, len(contents)
                archive.addfile(member, io.BytesIO(contents))

    def test_complete_release_includes_companion_checksum(self):
        release.check_assets("v1.2.3", self.root)
        lines = (self.root / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(lines), 10)
        self.assertTrue(any(line.endswith("murmur-companion-v1.2.3.fap") for line in lines))

    def test_missing_companion_blocks_release(self):
        (self.root / "murmur-companion-v1.2.3.fap").unlink()
        with self.assertRaises(ValueError): release.check_assets("v1.2.3", self.root)

    def test_empty_asset_blocks_release(self):
        (self.root / "Murmur_1.2.3_amd64.deb").write_bytes(b"")
        with self.assertRaises(ValueError): release.check_assets("v1.2.3", self.root)

    def test_non_executable_archive_blocks_release(self):
        self.archive(mode=0o644)
        with self.assertRaises(ValueError): release.check_assets("v1.2.3", self.root)

    def test_different_portable_binary_blocks_release(self):
        self.archive(data=b"stale")
        with self.assertRaises(ValueError): release.check_assets("v1.2.3", self.root)

    def test_empty_portable_app_blocks_release(self):
        self.archive(data=b"")
        with self.assertRaisesRegex(ValueError, "nonempty executable"):
            release.check_assets("v1.2.3", self.root)

    def test_second_engine_copy_in_archive_blocks_release(self):
        self.archive(extra=(("murmur-engine", b"engine"),))
        self.evidence()
        with self.assertRaisesRegex(ValueError, "only the AppImage"):
            release.check_assets("v1.2.3", self.root)

    def test_cudnn_runtime_probe_is_not_accepted(self):
        evidence = json.loads((self.root / "GPU-VALIDATION.json").read_text())
        evidence["runtime_probe"]["libcudnn.so.9"] = "/tmp/data/murmur/components/cuda/1.2.3/libcudnn.so.9"
        (self.root / "GPU-VALIDATION.json").write_text(json.dumps(evidence))
        with self.assertRaisesRegex(ValueError, "origin evidence"):
            release.check_assets("v1.2.3", self.root)

    def test_unexpected_platform_blocks_release(self):
        (self.root / "companion.apk").write_bytes(b"unexpected")
        with self.assertRaises(ValueError): release.check_assets("v1.2.3", self.root)

    def test_stale_gpu_evidence_blocks_release(self):
        (self.root / "GPU-VALIDATION.json").write_text("{}")
        with self.assertRaises(ValueError): release.check_assets("v1.2.3", self.root)

    def test_modified_package_invalidates_gpu_evidence(self):
        (self.root / "Murmur_1.2.3_amd64.deb").write_bytes(b"changed package")
        with self.assertRaisesRegex(ValueError, "GPU validation is not tied"):
            release.check_assets("v1.2.3", self.root)

    def test_missing_gpu_evidence_blocks_release(self):
        (self.root / "GPU-VALIDATION.json").unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            release.check_assets("v1.2.3", self.root)

    def test_oversized_asset_blocks_release(self):
        with (self.root / "Murmur_1.2.3_amd64.deb").open("wb") as stream:
            stream.truncate(release.DEB_BUDGET + 1)
        with self.assertRaisesRegex(ValueError, "over its 125 MiB budget"):
            release.check_assets("v1.2.3", self.root)

    def test_standalone_binaries_block_release(self):
        for name in ("murmur", "murmur-engine"):
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(b"unwanted standalone download")
                try:
                    with self.assertRaisesRegex(ValueError, "unexpected"):
                        release.check_assets("v1.2.3", self.root)
                finally:
                    path.unlink()


    def test_packaged_cuda_runtime_is_not_accepted_as_evidence(self):
        evidence = json.loads((self.root / "GPU-VALIDATION.json").read_text())
        evidence["runtime_probe"] = {n: f"/tmp/runtime/usr/lib/murmur/cuda/{n}" for n in ("libcublas.so.12", "libcublasLt.so.12")}
        (self.root / "GPU-VALIDATION.json").write_text(json.dumps(evidence))
        with self.assertRaisesRegex(ValueError, "origin evidence"):
            release.check_assets("v1.2.3", self.root)

    def test_manifest_must_point_at_this_release(self):
        for mutate, message in (
            (lambda m: m["components"][0].update(urls=["https://github.com/Zethrus/Murmur/releases/download/v1.2.2/murmur-engine-linux-x86_64-1.2.3.zip"]), "this release"),
            (lambda m: m["components"][1].update(sha256="0" * 64), "SHA-256"),
            (lambda m: m["components"][1].update(size_bytes=1), "size"),
            (lambda m: m["components"].pop(1), "engine and CUDA"),
            (lambda m: m["components"][2]["files"][0].update(urls=["https://example.com/model.bin"]), "huggingface"),
            (lambda m: m.update(schema_version=2), "schema 1"),
        ):
            with self.subTest(message=message):
                self.manifest(mutate=mutate)
                self.evidence()
                with self.assertRaisesRegex(ValueError, message):
                    release.check_assets("v1.2.3", self.root)

    def test_missing_signature_or_certificate_blocks_release(self):
        for name in ("components-manifest.json.sig", "murmur-release.asc"):
            with self.subTest(name=name):
                data = (self.root / name).read_bytes()
                (self.root / name).unlink()
                with self.assertRaisesRegex(ValueError, "missing"):
                    release.check_assets("v1.2.3", self.root)
                (self.root / name).write_bytes(data)


class WindowsReleaseTests(ReleaseTests):
    """Same Linux release plus the Windows set built on Edison (ENABLE_WINDOWS)."""
    WINDOWS_ZIPS = ("murmur-engine-windows-x86_64-1.2.3.zip", "murmur-cuda-windows-x86_64-1.2.3.zip")

    def setUp(self):
        super().setUp()
        import zipfile
        (self.root / "Murmur_1.2.3_x64-setup.exe").write_bytes(b"MZ installer")
        for name in self.WINDOWS_ZIPS:
            (self.root / name).write_bytes(name.encode())
        self.portable({"murmur.exe": b"MZ app", "README.txt": b"Engine downloads on first run."})
        self.windows_manifest()
        self.windows_evidence()
        self.zipfile = zipfile

    def portable(self, members):
        import zipfile
        with zipfile.ZipFile(self.root / "murmur-windows-x86_64.zip", "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)

    def windows_manifest(self, mutate=None):
        base = "https://github.com/Zethrus/Murmur/releases/download/v1.2.3/"
        def add(manifest):
            for id, name in zip(("engine", "cuda"), self.WINDOWS_ZIPS):
                data = (self.root / name).read_bytes()
                manifest["components"].append({"id": id, "os": "windows", "archive": "zip", "urls": [base + name],
                                               "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            if mutate:
                mutate(manifest)
        self.manifest(mutate=add)
        self.evidence()

    def windows_evidence(self, modules=None, **overrides):
        names = ["Murmur_1.2.3_x64-setup.exe", "murmur-windows-x86_64.zip", *self.WINDOWS_ZIPS, "components-manifest.json"]
        cuda = "C:\\Users\\ci\\AppData\\Local\\Temp\\p\\Murmur\\components\\cuda\\1.2.3"
        evidence = {"assets": {n: hashlib.sha256((self.root / n).read_bytes()).hexdigest() for n in names},
                    "gpu": {"status": {"device": "cuda", "cpu_fallback": False}, "warm_runs_ms": [1] * 10,
                            "modules": modules if modules is not None else [
                                f"{cuda}\\cublas64_12.dll", f"{cuda}\\cublasLt64_12.dll", "C:\\Windows\\System32\\nvcuda.dll"]},
                    "auto": {"status": {"device": "cuda", "cpu_fallback": False}},
                    "driver": "RTX, 580", "origin_validated": True}
        evidence.update(overrides)
        (self.root / "GPU-VALIDATION-windows.json").write_text(json.dumps(evidence))

    def test_windows_release_with_gpu_evidence_passes(self):
        release.check_assets("v1.2.3", self.root, windows=True, windows_gpu=True)
        sums = (self.root / "SHA256SUMS").read_text()
        for name in ("Murmur_1.2.3_x64-setup.exe", "GPU-VALIDATION-windows.json", *self.WINDOWS_ZIPS):
            self.assertIn(name, sums)

    def test_windows_release_without_gpu_run(self):
        (self.root / "GPU-VALIDATION-windows.json").unlink()
        release.check_assets("v1.2.3", self.root, windows=True)
        with self.assertRaises(ValueError):
            release.check_assets("v1.2.3", self.root, windows=True, windows_gpu=True)

    def test_windows_assets_without_the_flag_are_unexpected(self):
        with self.assertRaisesRegex(ValueError, "Asset mismatch"):
            release.check_assets("v1.2.3", self.root)

    def test_missing_windows_asset_blocks_release(self):
        (self.root / "murmur-cuda-windows-x86_64-1.2.3.zip").unlink()
        with self.assertRaisesRegex(ValueError, "Asset mismatch"):
            release.check_assets("v1.2.3", self.root, windows=True)

    def test_manifest_must_list_windows_components(self):
        self.manifest()
        self.evidence()
        self.windows_evidence()
        with self.assertRaisesRegex(ValueError, "Windows engine and CUDA"):
            release.check_assets("v1.2.3", self.root, windows=True, windows_gpu=True)

    def test_windows_zip_must_match_manifest(self):
        self.windows_manifest(lambda m: m["components"][-1].update(sha256="0" * 64))
        self.windows_evidence()
        with self.assertRaisesRegex(ValueError, "cuda size or SHA-256"):
            release.check_assets("v1.2.3", self.root, windows=True, windows_gpu=True)

    def test_portable_zip_must_not_carry_an_engine(self):
        self.portable({"murmur.exe": b"MZ app", "README.txt": b"x", "engine/murmur-engine.exe": b"MZ"})
        self.windows_evidence()
        with self.assertRaisesRegex(ValueError, "portable zip must contain only"):
            release.check_assets("v1.2.3", self.root, windows=True, windows_gpu=True)
        self.portable({"murmur.exe": b"not pe", "README.txt": b"x"})
        self.windows_evidence()
        with self.assertRaisesRegex(ValueError, "not a Windows executable"):
            release.check_assets("v1.2.3", self.root, windows=True, windows_gpu=True)

    def test_windows_gpu_evidence_must_show_add_on_cublas_and_host_driver(self):
        cases = {
            "did not load from the CUDA add-on": ["C:\\Program Files\\NVIDIA\\cublas64_12.dll",
                                                  "C:\\x\\components\\cuda\\1\\cublasLt64_12.dll",
                                                  "C:\\Windows\\System32\\nvcuda.dll"],
            "nvcuda.dll": ["C:\\x\\components\\cuda\\1\\cublas64_12.dll",
                           "C:\\x\\components\\cuda\\1\\cublasLt64_12.dll"],
            "cuDNN/NVRTC loaded": ["C:\\x\\components\\cuda\\1\\cublas64_12.dll",
                                   "C:\\x\\components\\cuda\\1\\cublasLt64_12.dll",
                                   "C:\\Windows\\System32\\nvcuda.dll",
                                   "C:\\x\\components\\engine\\1\\_internal\\ctranslate2\\cudnn64_9.dll"],
        }
        for message, modules in cases.items():
            with self.subTest(message=message):
                self.windows_evidence(modules=modules)
                with self.assertRaisesRegex(ValueError, message):
                    release.check_assets("v1.2.3", self.root, windows=True, windows_gpu=True)

    def test_windows_cpu_fallback_or_stale_evidence_blocks_release(self):
        self.windows_evidence(auto={"status": {"device": "cpu", "cpu_fallback": True}})
        with self.assertRaisesRegex(ValueError, "auto CUDA validation"):
            release.check_assets("v1.2.3", self.root, windows=True, windows_gpu=True)
        self.windows_evidence()
        (self.root / "Murmur_1.2.3_x64-setup.exe").write_bytes(b"MZ rebuilt")
        with self.assertRaisesRegex(ValueError, "not tied"):
            release.check_assets("v1.2.3", self.root, windows=True, windows_gpu=True)


# The Linux-only cases above do not apply with the Windows set present.
for _name in [name for name in vars(ReleaseTests) if name.startswith("test_")]:
    setattr(WindowsReleaseTests, _name, None)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def sized(self, name, size):
        path = self.root / name
        with path.open("wb") as stream:
            stream.truncate(size)
        return path

    def test_budgets_are_the_documented_values(self):
        self.assertEqual((release.APPIMAGE_BUDGET, release.DEB_BUDGET, release.TARBALL_BUDGET,
                          release.ENGINE_ZIP_BUDGET, release.CUDA_ZIP_BUDGET),
                         (125 * 1024 ** 2, 125 * 1024 ** 2, 130 * 1024 ** 2, 130 * 1024 ** 2, 520 * 1024 ** 2))

    def test_windows_budgets_are_the_documented_values(self):
        self.assertEqual((release.WINDOWS_INSTALLER_BUDGET, release.WINDOWS_PORTABLE_BUDGET,
                          release.WINDOWS_ENGINE_ZIP_BUDGET, release.WINDOWS_CUDA_ZIP_BUDGET),
                         (35 * 1024 ** 2, 35 * 1024 ** 2, 130 * 1024 ** 2, 520 * 1024 ** 2))

    def test_artifacts_at_budget_pass_and_one_byte_over_fails(self):
        for name, limit in (("Murmur_1.2.3_amd64.AppImage", release.APPIMAGE_BUDGET),
                            ("Murmur_1.2.3_amd64.deb", release.DEB_BUDGET),
                            ("murmur-linux-x86_64.tar.gz", release.TARBALL_BUDGET),
                            ("murmur-engine-linux-x86_64-1.2.3.zip", release.ENGINE_ZIP_BUDGET),
                            ("murmur-cuda-linux-x86_64-1.2.3.zip", release.CUDA_ZIP_BUDGET),
                            ("components-manifest.json.sig", 1024 ** 2), ("murmur-release.asc", 1024 ** 2),
                            ("Murmur_1.2.3_x64-setup.exe", release.WINDOWS_INSTALLER_BUDGET),
                            ("murmur-windows-x86_64.zip", release.WINDOWS_PORTABLE_BUDGET),
                            ("murmur-engine-windows-x86_64-1.2.3.zip", release.WINDOWS_ENGINE_ZIP_BUDGET),
                            ("murmur-cuda-windows-x86_64-1.2.3.zip", release.WINDOWS_CUDA_ZIP_BUDGET),
                            ("GPU-VALIDATION-windows.json", 1024 ** 2)):
            with self.subTest(name=name):
                release.check_budgets([self.sized(name, limit)])
                with self.assertRaisesRegex(ValueError, "budget"):
                    release.check_budgets([self.sized(name, limit + 1)])

    def test_unknown_release_file_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "No size budget"):
            release.check_budgets([self.sized("murmur-engine", 10)])
        with self.assertRaisesRegex(ValueError, "No size budget"):
            release.check_budgets([self.sized("other.zip", 10)])
        release.check_budgets([self.sized("SHA256SUMS", 10)])

    def test_appimage_offset_follows_elf_section_headers(self):
        header = bytearray(64)
        header[:5] = b"\x7fELF\x02"
        struct.pack_into("<Q", header, 0x28, 1000)
        struct.pack_into("<HH", header, 0x3A, 64, 3)
        image = self.root / "Murmur.AppImage"
        image.write_bytes(bytes(header) + b"squashfs")
        self.assertEqual(release.appimage_offset(image), 1192)
        (self.root / "bad.AppImage").write_bytes(b"MZ" + bytes(62))
        with self.assertRaisesRegex(ValueError, "64-bit ELF"):
            release.appimage_offset(self.root / "bad.AppImage")

    def test_size_table_reports_download_unpacked_and_budget(self):
        archive = self.root / "murmur-linux-x86_64.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            member = tarfile.TarInfo("murmur")
            member.size = 3 * 1024 ** 2
            stream.addfile(member, io.BytesIO(bytes(member.size)))
        engine = self.root / "murmur-engine"
        engine.mkdir()
        (engine / "lib.so").write_bytes(bytes(1024 ** 2))
        table = release.size_table([archive, engine]).splitlines()
        self.assertEqual(table[0], "| Artifact | Download (MiB) | Unpacked (MiB) | Budget (MiB) |")
        self.assertRegex(table[2], r"^\| `murmur-linux-x86_64.tar.gz` \| 0\.\d \| 3\.0 \| 130 \|$")
        self.assertEqual(table[3], "| `murmur-engine` | — | 1.0 | — |")
