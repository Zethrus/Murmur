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


# Explicit fixture requirements, independent of the auditor's own option lists.
REQUIRED_ON = ('GGML_AVX', 'GGML_AVX2', 'GGML_FMA', 'GGML_F16C', 'GGML_BMI2', 'GGML_SSE42')
REQUIRED_OFF = ('GGML_NATIVE', 'GGML_AVX512', 'GGML_AVX512_BF16',
                'GGML_AVX512_VBMI', 'GGML_AVX512_VNNI', 'GGML_AVX_VNNI',
                'GGML_CPU_ALL_VARIANTS', 'GGML_AMX_BF16', 'GGML_AMX_INT8', 'GGML_AMX_TILE')
CONTRACT = (''.join(f'{name}:BOOL=ON\n' for name in REQUIRED_ON)
            + ''.join(f'{name}:BOOL=OFF\n' for name in REQUIRED_OFF))
FLAGS = '-m64 -march=x86-64 -mavx -mavx2 -mfma -mf16c -mbmi2 -msse4.2'

class AuditTests(unittest.TestCase):
    def test_effective_compiler_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = pathlib.Path(tmp) / 'CMakeCache.txt'
            cache.write_text(CONTRACT)
            flags = cache.parent / 'flags.make'
            (cache.parent / 'extra.rsp').write_text('-mavx512f')
            for value in ('@extra.rsp', 'CXX_FLAGS=@extra.rsp', '"@extra.rsp"',
                          '-Xclang -target-feature -Xclang +avx512f'):
                with self.subTest(value=value):
                    flags.write_text('CXX_FLAGS = ' + value)
                    with self.assertRaises(ValueError):
                        audit.audit_cache(cache)
            flags.write_text('CXX_FLAGS = @missing.rsp')
            with self.assertRaisesRegex(ValueError, 'unresolved'):
                audit.audit_cache(cache)
            (cache.parent / 'extra.rsp').write_text('@nested.rsp')
            (cache.parent / 'nested.rsp').write_text('-mavx512f')
            flags.write_text('CXX_FLAGS = @extra.rsp')
            with self.assertRaisesRegex(ValueError, 'unsupported CPU flag'):
                audit.audit_cache(cache)

    def test_quoted_and_conflicting_response_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            cache = root / 'CMakeCache.txt'
            cache.write_text(CONTRACT)
            directory = root / 'CMakeFiles' / 'cpu.dir'
            directory.mkdir(parents=True)
            flags = directory / 'flags.make'
            # GCC interprets both quoted forms as response files in the build cwd.
            (root / 'unsafe flags.rsp').write_text('-mavx512f')
            for value in ("@'unsafe flags.rsp'", '@"unsafe flags.rsp"'):
                with self.subTest(value=value):
                    flags.write_text('CXX_FLAGS = ' + value)
                    with self.assertRaises(ValueError):
                        audit.audit_cache(cache)
            (directory / 'same.rsp').write_text('-msse2')
            (root / 'same.rsp').write_text('-mavx512f')
            flags.write_text('CXX_FLAGS = @same.rsp')
            with self.assertRaises(ValueError):
                audit.audit_cache(cache)
            (root / 'same.rsp').unlink()
            with self.assertRaises(ValueError):
                audit.audit_cache(cache)

    def test_compile_commands_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            cache = root / 'CMakeCache.txt'
            cache.write_text(CONTRACT)
            directory = root / 'other'
            directory.mkdir()
            (root / 'same.rsp').write_text('-msse2')
            (directory / 'same.rsp').write_text('-mavx512f')
            commands = root / 'compile_commands.json'
            commands.write_text(json.dumps([{'directory': str(directory), 'command': "c++ @'same.rsp'"}]))
            with self.assertRaisesRegex(ValueError, 'unsupported CPU flag'):
                audit.audit_cache(cache)
            commands.write_text(json.dumps([{'directory': 'other', 'arguments': ['c++', '@same.rsp']}]))
            with self.assertRaisesRegex(ValueError, 'ambiguous compiler working directory'):
                audit.audit_cache(cache)
            commands.write_text(json.dumps([{'directory': str(directory), 'arguments': ['c++', '@same.rsp']}]))
            with self.assertRaisesRegex(ValueError, 'unsupported CPU flag'):
                audit.audit_cache(cache)
            commands.write_text(json.dumps([{'directory': str(directory), 'command': 'c++ -msse2',
                                              'arguments': ['c++', '-mavx512f']}]))
            with self.assertRaisesRegex(ValueError, 'ambiguous compiler arguments'):
                audit.audit_cache(cache)

    def test_independent_baseline_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = pathlib.Path(tmp) / 'CMakeCache.txt'
            (cache.parent / 'flags.make').write_text('CXX_FLAGS = ' + FLAGS)
            for missing in REQUIRED_ON + REQUIRED_OFF:
                with self.subTest(missing=missing):
                    cache.write_text('\n'.join(line for line in CONTRACT.splitlines() if not line.startswith(missing + ':')))
                    with self.assertRaises(ValueError):
                        audit.audit_cache(cache)

    def test_unsupported_instruction_families(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = pathlib.Path(tmp) / 'CMakeCache.txt'
            cache.write_text(CONTRACT)
            flags = cache.parent / 'flags.make'
            for flag in ('-msse3', '-mssse3', '-mpopcnt', '-maes', '-march=x86-64-v2',
                         '-mavx512f', '-mbmi', '-mcpu=native', '-march=native', '/arch:AVX512',
                         '-mno-avx', '-mno-avx2', '-mno-fma', '-mno-f16c', '-mno-bmi2', '-mno-sse4.2',
                         '-munknown', '-Xclang -target-feature -Xclang -avx2'):
                with self.subTest(flag=flag):
                    flags.write_text('CXX_FLAGS = -m64 ' + flag)
                    with self.assertRaisesRegex(ValueError, 'unsupported CPU flag'):
                        audit.audit_cache(cache)
            flags.write_text('CXX_FLAGS = ' + FLAGS)
            self.assertEqual(audit.audit_cache(cache), 1)
            cache.write_text(cache.read_text().replace('GGML_AVX2:BOOL=ON', 'GGML_AVX2:BOOL=OFF'))
            with self.assertRaisesRegex(ValueError, 'GGML_AVX2 must be ON'):
                audit.audit_cache(cache)

    def test_contract_regressions_in_both_directions(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = pathlib.Path(tmp) / 'CMakeCache.txt'
            flags = cache.parent / 'flags.make'
            flags.write_text('CXX_FLAGS = ' + FLAGS)
            for name, old, new in [(name, 'ON', 'OFF') for name in REQUIRED_ON] + [
                    (name, 'OFF', 'ON') for name in REQUIRED_OFF]:
                with self.subTest(name=name):
                    cache.write_text(CONTRACT.replace(f'{name}:BOOL={old}', f'{name}:BOOL={new}'))
                    with self.assertRaisesRegex(ValueError, name + ' must be ' + old):
                        audit.audit_cache(cache)
            for name in ('GGML_AVX512_NEW', 'GGML_AMX_NEW'):
                cache.write_text(CONTRACT + f'{name}:BOOL=ON\n')
                with self.assertRaisesRegex(ValueError, name + ' must be OFF'):
                    audit.audit_cache(cache)
            cache.write_text(CONTRACT)
            flags.write_text('CXX_FLAGS = -mavx2')
            with self.assertRaisesRegex(ValueError, 'must contain -march=x86-64'):
                audit.audit_cache(cache)

    def test_all_flag_sources_are_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            cache = root / 'CMakeCache.txt'
            cache.write_text(CONTRACT)
            (root / 'flags.make').write_text('CXX_FLAGS = ' + FLAGS)
            for name, contents in (
                ('build.ninja', 'FLAGS = -mavx512f'),
                ('compile_commands.json', json.dumps([{'directory': str(root), 'command': 'c++ -mno-avx2'}])),
                ('CMakeCache.txt', CONTRACT + 'CMAKE_CXX_FLAGS:STRING=-march=native\n'),
            ):
                with self.subTest(name=name):
                    source = root / name
                    source.write_text(contents)
                    with self.assertRaisesRegex(ValueError, 'unsupported CPU flag'):
                        audit.audit_cache(cache)
                    if source != cache:
                        source.unlink()
                    cache.write_text(CONTRACT)
            (root / 'safe.rsp').write_text(FLAGS)
            (root / 'flags.make').write_text('CXX_FLAGS = @safe.rsp')
            self.assertEqual(audit.audit_cache(cache), 1)

    def test_windows_arch_switch_satisfies_the_baseline(self):
        # ggml emits /arch:AVX2 for MSVC instead of -march=x86-64; the audit must accept it
        # and still reject an AVX-512 or missing architecture switch.
        with tempfile.TemporaryDirectory() as tmp:
            cache = pathlib.Path(tmp) / 'CMakeCache.txt'
            cache.write_text(CONTRACT)
            flags = cache.parent / 'flags.make'
            flags.write_text('CXX_FLAGS = /arch:AVX2 /O2 /EHsc')
            self.assertEqual(audit.audit_cache(cache, windows=True), 1)
            flags.write_text('CXX_FLAGS = /arch:AVX512')
            with self.assertRaisesRegex(ValueError, 'unsupported CPU flag'):
                audit.audit_cache(cache, windows=True)
            flags.write_text('CXX_FLAGS = /O2 /EHsc')
            with self.assertRaisesRegex(ValueError, 'must contain -march=x86-64 or /arch:AVX2'):
                audit.audit_cache(cache, windows=True)

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
            cache.write_text(CONTRACT)
            (cache.parent / 'flags.make').write_text('CXX_FLAGS = ' + FLAGS)
            stale = root / 'build' / 'llama-cpp-sys-2-cccccccccccccccc' / 'out' / 'build'
            stale.mkdir(parents=True)
            (stale / 'CMakeCache.txt').write_text('GGML_AVX2:BOOL=OFF')
            self.assertEqual(audit.linked_cache(root, artifact), cache)
            self.assertEqual(audit.audit_cache(cache), 1)
            # target/<profile>/murmur after `tauri build` patched the bundle type still matches
            # its Cargo binary; any other difference does not.
            artifact.write_bytes(b'fake executable __TAURI_BUNDLE_TYPE_VAR_UNK end')
            bundled = root / ('murmur.exe' if audit.sys.platform == 'win32' else 'murmur')
            bundled.write_bytes(b'fake executable __TAURI_BUNDLE_TYPE_VAR_NSS end')
            self.assertEqual(audit.linked_cache(root, bundled), cache)
            # A few patched bytes elsewhere (Windows rewrites a PE section) still match ...
            bundled.write_bytes(b'fake executable __TAURI_BUNDLE_TYPE_VAR_NSS enD')
            self.assertEqual(audit.linked_cache(root, bundled), cache)
            # ... a different build of the same size does not.
            artifact.write_bytes(bytes(200))
            bundled.write_bytes(bytes(range(200)))
            with self.assertRaisesRegex(ValueError, 'Cannot uniquely match.*bytes differ'):
                audit.linked_cache(root, bundled)
            cache.write_text(cache.read_text().replace('GGML_AVX2:BOOL=ON', 'GGML_AVX2:BOOL=OFF'))
            with self.assertRaisesRegex(ValueError, 'GGML_AVX2 must be ON'):
                audit.audit_cache(audit.linked_cache(root, artifact))


if __name__ == '__main__':
    unittest.main()
