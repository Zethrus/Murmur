"""Failure contracts for the bounded cuBLAS-only CUDA collector."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import linux_cuda_runtime as cuda


class FakeDistribution:
    def __init__(self, root, component, version, names, notice=True):
        self.root, self.version = root, version
        self.files = []
        for name in names:
            file = Path(f"nvidia/{component}/lib/{name}")
            path = root / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
            self.files.append(file)
        if notice:
            file = Path(f"{component}.dist-info/licenses/License.txt")
            path = root / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("NVIDIA test notice")
            self.files.append(file)

    def locate_file(self, file):
        return self.root / file


class CudaRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.appdir = self.root / "AppDir"
        self.lock = self.root / "lock.txt"
        # NVRTC stays in the lock as a pip dependency of the cuBLAS wheel; it is not shipped.
        self.lock.write_text("".join(f"{name}=={version} --hash=sha256:{'a'*64}\n" for name, version in (
            ("nvidia-cublas-cu12", "12.9.3.13"),
            ("nvidia-cuda-nvrtc-cu12", "12.9.86"))))
        self.distributions = {
            "nvidia-cublas-cu12": FakeDistribution(self.root / "selected", "cublas", "12.9.3.13",
                ["libcublas.so.12", "libcublasLt.so.12", "libnvblas.so.12"]),
        }

    def stage(self):
        with patch.object(cuda.metadata, "distribution", side_effect=self.distributions.get), \
             patch.object(cuda.subprocess, "run") as run, \
             patch.dict(os.environ, {"LD_LIBRARY_PATH": str(self.root / "polluted")}) as _:
            run.return_value.stdout = ""
            cuda.stage(self.appdir, self.lock)

    def test_selected_metadata_ignores_ambient_cuda_paths(self):
        polluted = self.root / "polluted"
        polluted.mkdir()
        (polluted / "libcublas.so.12").write_text("wrong")
        self.stage()
        manifest = json.loads((self.appdir / cuda.DOC / "cuda-manifest.json").read_text())
        self.assertEqual(set(manifest["packages"]), {"nvidia-cublas-cu12"})
        self.assertEqual((self.appdir / cuda.CUDA / "libcublas.so.12").read_bytes(), b"libcublas.so.12")
        (self.appdir / cuda.CUDA / "libcublasLt.so.12").unlink()
        with self.assertRaises(RuntimeError): cuda.verify(self.appdir, self.lock)

    def test_only_cublas_ships(self):
        self.stage()
        shipped = {p.name for p in (self.appdir / cuda.CUDA).iterdir()}
        self.assertEqual(shipped, {"libcublas.so.12", "libcublasLt.so.12"})
        manifest = json.loads((self.appdir / cuda.DOC / "cuda-manifest.json").read_text())
        self.assertEqual(set(manifest["files"]), shipped)
        self.assertEqual(set(manifest["notices"]), {"nvidia-cublas-cu12.License.txt"})

    def test_extra_cudnn_or_nvrtc_library_fails_verification(self):
        self.stage()
        for name in ("libcudnn.so.9", "libnvrtc.so.12", "libnvblas.so.12"):
            with self.subTest(name=name):
                extra = self.appdir / cuda.CUDA / name
                extra.write_bytes(b"extra")
                with patch.object(cuda.subprocess, "run") as run:
                    run.return_value.stdout = ""
                    with self.assertRaises(RuntimeError):
                        cuda.verify(self.appdir, self.lock)
                extra.unlink()

    def test_missing_notice_fails_collection(self):
        self.distributions["nvidia-cublas-cu12"].files = [f for f in self.distributions["nvidia-cublas-cu12"].files if "licenses" not in str(f)]
        with self.assertRaisesRegex(RuntimeError, "Missing NVIDIA notice"):
            self.stage()

    def test_tampered_notice_and_lock_fail_verification(self):
        self.stage()
        notice = self.appdir / cuda.DOC / "nvidia-cublas-cu12.License.txt"
        notice.write_text("changed")
        with self.assertRaisesRegex(RuntimeError, "notice checksum mismatch"):
            cuda.verify(self.appdir, self.lock)
        notice.write_text("NVIDIA test notice")
        self.lock.write_text(self.lock.read_text() + "# changed\n")
        with self.assertRaisesRegex(RuntimeError, "release lock mismatch"):
            cuda.verify(self.appdir, self.lock)

    def test_missing_library_fails(self):
        self.distributions["nvidia-cublas-cu12"].files = []
        with self.assertRaisesRegex(RuntimeError, "Missing cublas runtime files"):
            self.stage()

    def test_wheel_without_cublaslt_fails(self):
        dist = self.distributions["nvidia-cublas-cu12"]
        dist.files = [f for f in dist.files if f.name != "libcublasLt.so.12"]
        with self.assertRaisesRegex(RuntimeError, "Missing required CUDA libraries"):
            self.stage()

    def test_unbundled_elf_dependency_fails(self):
        self.stage()
        def linked(*args, **kwargs):
            class Result:
                stdout = "0x1 (NEEDED) Shared library: [libcudart.so.12]"
            return Result()
        with patch.object(cuda.subprocess, "run", side_effect=linked):
            with self.assertRaisesRegex(RuntimeError, "Unbundled CUDA ELF dependency"):
                cuda.verify(self.appdir, self.lock)

    def test_host_cuda_dependency_with_space_in_path_fails(self):
        self.stage()
        def linked(*args, **kwargs):
            class Result:
                stdout = ("libcublas.so.12 => /tmp/host cuda/libcublas.so.12 (0x123)\n"
                          if args[0][0] == "ldd" else "")
            return Result()
        with patch.object(cuda.subprocess, "run", side_effect=linked):
            with self.assertRaisesRegex(RuntimeError, "loaded from host"):
                cuda.verify(self.appdir, self.lock)


if __name__ == "__main__":
    unittest.main()
