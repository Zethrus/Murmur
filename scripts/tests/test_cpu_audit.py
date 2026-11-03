"""Negative fixtures for the linked llama CPU baseline gate."""
import importlib.util
import json
import pathlib
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'audit-llama-cpu.py'
spec = importlib.util.spec_from_file_location('cpu_audit', SCRIPT)
assert spec is not None and spec.loader is not None
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class AuditTests(unittest.TestCase):
    def test_unsupported_instruction_families(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = pathlib.Path(tmp) / 'CMakeCache.txt'
            cache.write_text(''.join(f'{name}:BOOL=OFF\n' for name in audit.BASELINE_OFF))
            flags = cache.parent / 'flags.make'
            for flag in ('-msse3', '-mssse3', '-mpopcnt', '-maes', '-march=x86-64-v2',
                         '-mavx2', '-mfma', '-mbmi', '-mcpu=native', '/arch:AVX2'):
                with self.subTest(flag=flag):
                    flags.write_text('CXX_FLAGS = -m64 ' + flag)
                    with self.assertRaisesRegex(ValueError, 'unsupported CPU flag'):
                        audit.audit_cache(cache)
            flags.write_text('CXX_FLAGS = -m64 -march=x86-64 -msse2')
            self.assertEqual(audit.audit_cache(cache), 1)
            cache.write_text(cache.read_text().replace('GGML_AVX:BOOL=OFF', 'GGML_AVX:BOOL=ON'))
            with self.assertRaisesRegex(ValueError, 'GGML_AVX must be OFF'):
                audit.audit_cache(cache)

    def test_stale_newer_cache_is_not_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            fp = root / '.fingerprint'
            deps = root / 'deps'
            deps.mkdir()
            artifact = deps / 'murmur-0123456789abcdef'
            artifact.write_bytes(b'fake executable')
            def fingerprint(path, number):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(number.to_bytes(8, 'little').hex())
            binary = fp / 'murmur-0123456789abcdef' / 'bin-murmur.json'
            binary.parent.mkdir(parents=True)
            binary.write_text(json.dumps({'deps': [[0, 'llama_cpp_sys_2', False, 11]]}))
            sys = fp / 'llama-cpp-sys-2-aaaaaaaaaaaaaaaa' / 'lib-llama_cpp_sys_2'
            fingerprint(sys, 11)
            sys.with_suffix('.json').write_text(json.dumps({'deps': [[0, 'build_script_build', False, 22]]}))
            run = fp / 'llama-cpp-sys-2-bbbbbbbbbbbbbbbb' / 'run-build-script-build-script-build'
            fingerprint(run, 22)
            good = root / 'build' / run.parent.name
            (good / 'out' / 'build').mkdir(parents=True)
            (good / 'output').write_text('cargo:rustc-link-lib=static=ggml-cpu')
            cache = good / 'out' / 'build' / 'CMakeCache.txt'
            cache.write_text(''.join(f'{name}:BOOL=OFF\n' for name in audit.BASELINE_OFF))
            (cache.parent / 'flags.make').write_text('CXX_FLAGS = -msse2')
            stale = root / 'build' / 'llama-cpp-sys-2-cccccccccccccccc' / 'out' / 'build'
            stale.mkdir(parents=True)
            (stale / 'CMakeCache.txt').write_text('GGML_AVX:BOOL=ON')
            self.assertEqual(audit.linked_cache(root, artifact), cache)
            self.assertEqual(audit.audit_cache(cache), 1)
            cache.write_text(cache.read_text().replace('GGML_AVX:BOOL=OFF', 'GGML_AVX:BOOL=ON'))
            with self.assertRaisesRegex(ValueError, 'GGML_AVX must be OFF'):
                audit.audit_cache(audit.linked_cache(root, artifact))


if __name__ == '__main__':
    unittest.main()
