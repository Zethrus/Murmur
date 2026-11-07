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
        for name in ("Murmur_1.2.3_amd64.deb", "Murmur_1.2.3_amd64.AppImage", "murmur-companion-v1.2.3.fap"):
            (self.root / name).write_bytes(b"artifact")
        self.archive()
        self.evidence()

    def evidence(self):
        linux = [p for p in self.root.iterdir() if p.name not in {"murmur-companion-v1.2.3.fap", "GPU-VALIDATION.json", "SHA256SUMS"}]
        assets = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in linux}
        (self.root / "GPU-VALIDATION.json").write_text(json.dumps({"assets": assets,
            "gpu": {"status": {"device": "cuda", "cpu_fallback": False}, "warm_runs_ms": [1] * 10,
                    "libraries": ["/tmp/runtime/usr/lib/murmur/cuda/libcublas.so.12", "/usr/lib/libcuda.so.1"]},
            "auto": {"status": {"device": "cuda", "cpu_fallback": False}},
            "driver": "test GPU, test driver", "origin_validated": True,
            "runtime_probe": {name: f"/tmp/runtime/usr/lib/murmur/cuda/{name}" for name in
                ("libcublas.so.12", "libcublasLt.so.12")}}))

    def archive(self, mode=0o755, data=b"artifact", extra=()):
        with tarfile.open(self.root / "murmur-linux-x86_64.tar.gz", "w:gz") as archive:
            for name, contents in (("murmur", data), *extra):
                member = tarfile.TarInfo(name)
                member.mode, member.size = mode, len(contents)
                archive.addfile(member, io.BytesIO(contents))

    def test_complete_release_includes_companion_checksum(self):
        release.check_assets("v1.2.3", self.root)
        lines = (self.root / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(lines), 5)
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
        evidence["runtime_probe"]["libcudnn.so.9"] = "/tmp/runtime/usr/lib/murmur/cuda/libcudnn.so.9"
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
        with self.assertRaisesRegex(ValueError, "over its 550 MiB budget"):
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

    def test_interim_budgets_are_the_documented_values(self):
        self.assertEqual((release.APPIMAGE_BUDGET, release.DEB_BUDGET, release.TARBALL_BUDGET),
                         (700 * 1024 ** 2, 550 * 1024 ** 2, 710 * 1024 ** 2))

    def test_artifacts_at_budget_pass_and_one_byte_over_fails(self):
        for name, limit in (("Murmur_1.2.3_amd64.AppImage", release.APPIMAGE_BUDGET),
                            ("Murmur_1.2.3_amd64.deb", release.DEB_BUDGET),
                            ("murmur-linux-x86_64.tar.gz", release.TARBALL_BUDGET)):
            with self.subTest(name=name):
                release.check_budgets([self.sized(name, limit)])
                with self.assertRaisesRegex(ValueError, "budget"):
                    release.check_budgets([self.sized(name, limit + 1)])

    def test_unknown_release_file_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "No size budget"):
            release.check_budgets([self.sized("murmur-engine", 10)])
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
        self.assertRegex(table[2], r"^\| `murmur-linux-x86_64.tar.gz` \| 0\.\d \| 3\.0 \| 710 \|$")
        self.assertEqual(table[3], "| `murmur-engine` | — | 1.0 | — |")
