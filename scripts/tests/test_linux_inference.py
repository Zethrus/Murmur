import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location("inference", Path(__file__).parents[1] / "test-linux-inference.py")
inference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inference)


class RuntimeProbeTests(unittest.TestCase):
    def test_relocated_python_keeps_own_library_without_host_cuda(self):
        cuda = Path("/package/usr/lib/murmur/cuda")
        paths = {name: str(cuda / name) for name in ("libcublas.so.12", "libcublasLt.so.12")}
        result = subprocess.CompletedProcess([], 0, json.dumps(paths), "")
        with patch.object(inference.sys, "base_prefix", "/relocated/python"), patch.dict(os.environ, {
            "LD_LIBRARY_PATH": "/host/cuda", "LD_PRELOAD": "/host/inject.so", "PYTHONPATH": "/host/python"
        }), patch.object(inference.subprocess, "run", return_value=result) as run:
            self.assertEqual(inference.probe_runtime_origin(cuda), paths)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["LD_LIBRARY_PATH"], f"{cuda}:/relocated/python/lib")
        self.assertNotIn("LD_PRELOAD", env)
        self.assertNotIn("PYTHONPATH", env)

    def test_external_cuda_still_rejected(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({name: f"/host/{name}" for name in
            ("libcublas.so.12", "libcublasLt.so.12")}), "")
        with patch.object(inference.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "origin invalid"):
                inference.probe_runtime_origin(Path("/package/cuda"))

    def test_probe_failure_includes_loader_diagnostic(self):
        result = subprocess.CompletedProcess([], 1, "", "ImportError: incompatible libpython")
        with patch.object(inference.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "incompatible libpython"):
                inference.probe_runtime_origin(Path("/package/cuda"))


class OriginTests(unittest.TestCase):
    cuda = Path("/package/usr/lib/murmur/cuda")

    def result(self, *extra):
        return {"libraries": [f"{self.cuda}/libcublas.so.12", f"{self.cuda}/libcublasLt.so.12",
                              "/usr/lib/x86_64-linux-gnu/libcuda.so.1", *extra]}

    def test_cublas_only_from_package_passes(self):
        inference.check_origin(self.result(), self.cuda)

    def test_any_cudnn_or_nvrtc_load_fails(self):
        for path in (f"{self.cuda}/libcudnn.so.9", "/opt/cuda/lib64/libnvrtc.so.12"):
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, "cuBLAS only"):
                inference.check_origin(self.result(path), self.cuda)

    def test_host_cublaslt_fails(self):
        result = self.result()
        result["libraries"][1] = "/usr/local/cuda/lib64/libcublasLt.so.12"
        with self.assertRaisesRegex(RuntimeError, "libcublasLt.so"):
            inference.check_origin(result, self.cuda)

    def test_matrix_covers_every_gpu_precision_with_and_without_vad(self):
        self.assertEqual(len(inference.MATRIX), 8)
        self.assertEqual({compute for compute, _ in inference.MATRIX},
                         {"int8_float16", "float16", "float32", "int8"})
