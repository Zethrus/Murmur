#!/usr/bin/env python3
"""Audit the llama.cpp native build actually referenced by a Cargo binary.

Cargo fingerprint dependency hashes identify the precise sys crate and build-script output;
cache timestamps are intentionally ignored. Run before stripping or packaging the binary.
"""
import argparse
import hashlib
import json
import pathlib
import re
import shlex
import sys

BASELINE_ON = ("GGML_AVX", "GGML_AVX2", "GGML_FMA", "GGML_F16C", "GGML_BMI2", "GGML_SSE42")
BASELINE_OFF = (
    "GGML_NATIVE", "GGML_AVX512", "GGML_AVX512_BF16", "GGML_AVX512_VBMI",
    "GGML_AVX512_VNNI", "GGML_AVX_VNNI", "GGML_CPU_ALL_VARIANTS",
    "GGML_AMX_BF16", "GGML_AMX_INT8", "GGML_AMX_TILE",
)
# Rust stays at SSE2; only the guarded native backend may use these extensions.
# Reject unknown -m CPU switches and flags that disable the required extensions.
ALLOWED_M = {"64", "sse", "sse2", "avx", "avx2", "fma", "f16c", "bmi2", "sse4.2", "no-native"}
REQUIRED_FEATURES = {"avx", "avx2", "fma", "f16c", "bmi2", "sse4.2"}
CPU_SWITCH = re.compile(r"(?<![\w-])(-march=[^\s\"']+|-mcpu=[^\s\"']+|-m[a-zA-Z0-9_.+-]+|/arch:[^\s\"']+)", re.I)


def fingerprint(path):
    return int.from_bytes(bytes.fromhex(path.read_text().strip()), "little")


def by_fingerprint(folder, glob, name, value):
    found = [p for p in folder.glob(glob) if p.name == name and fingerprint(p) == value]
    if len(found) != 1:
        raise ValueError(f"Expected one {name} fingerprint {value}, found {len(found)}")
    return found[0]


def dependency(path, name):
    matches = [d[3] for d in json.loads(path.read_text())["deps"] if d[1] == name]
    if len(matches) != 1:
        raise ValueError(f"{path}: expected one {name} dependency, found {len(matches)}")
    return matches[0]


# `tauri build` patches the main binary in place after Cargo links it, rewriting this marker's
# three-letter suffix to the bundle type (UNK -> NSS, DEB, RPM, APP, MSI); same length, so only
# those bytes differ from the Cargo binary in deps/.
BUNDLE_TYPE = re.compile(rb"__TAURI_BUNDLE_TYPE_VAR_[A-Z]{3}")


def _unpatched_digest(path):
    return hashlib.sha256(BUNDLE_TYPE.sub(b"__TAURI_BUNDLE_TYPE_VAR_UNK", path.read_bytes())).digest()


PATCH_TOLERANCE = 64


def _differing_bytes(a, b, limit=PATCH_TOLERANCE + 1, chunk=1 << 16):
    """Count differing bytes between two same-size files, stopping once `limit` is reached."""
    count = 0
    with a.open("rb") as left, b.open("rb") as right:
        while (x := left.read(chunk)):
            y = right.read(chunk)
            if x != y:
                count += sum(1 for i, j in zip(x, y) if i != j)
                if count >= limit:
                    return count
    return count


def linked_cache(target, artifact):
    target = target.resolve()
    artifact = artifact.resolve()
    deps = target / "deps"
    if artifact == target / ("murmur.exe" if sys.platform == "win32" else "murmur"):
        digest = _unpatched_digest(artifact)
        candidates = [p for p in deps.glob("murmur-*") if p.is_file() and p.suffix not in (".d", ".pdb")]
        same_size = [p for p in candidates if p.stat().st_size == artifact.stat().st_size]
        binaries = [p for p in same_size if _unpatched_digest(p) == digest]
        if not binaries:
            # On Windows the bundle type lives in a PE section that tauri rewrites, not only
            # the text marker; allow a few patched bytes. Separate builds differ far more.
            binaries = [p for p in same_size if _differing_bytes(artifact, p) <= PATCH_TOLERANCE]
        if len(binaries) != 1:
            details = ", ".join(f"{p.name} ({p.stat().st_size} B"
                                + (f", {_differing_bytes(artifact, p)} bytes differ" if p in same_size else "")
                                + ")" for p in candidates) or "none"
            raise ValueError(f"Cannot uniquely match {artifact} ({artifact.stat().st_size} B) to a Cargo "
                             f"binary (stripped or stale artifact?); candidates: {details}")
        artifact = binaries[0]
    if artifact.parent != deps or not re.fullmatch(r"murmur-[0-9a-f]{16}(?:\.exe)?", artifact.name):
        raise ValueError("Artifact must be a current unstripped Cargo binary or target/<profile>/murmur")
    binary_hash = artifact.stem.split("-", 1)[1]
    fp = target / ".fingerprint"
    binary = fp / f"murmur-{binary_hash}" / "bin-murmur.json"
    sys_hash = dependency(binary, "llama_cpp_sys_2")
    sys_fp = by_fingerprint(fp, "llama-cpp-sys-2-*/lib-llama_cpp_sys_2", "lib-llama_cpp_sys_2", sys_hash)
    run_hash = dependency(sys_fp.with_suffix(".json"), "build_script_build")
    run_fp = by_fingerprint(fp, "llama-cpp-sys-2-*/run-build-script-build-script-build",
                            "run-build-script-build-script-build", run_hash)
    output = target / "build" / run_fp.parent.name / "output"
    if not output.is_file():
        raise ValueError(f"Missing linked build-script output: {output}")
    cache = target / "build" / run_fp.parent.name / "out" / "build" / "CMakeCache.txt"
    if not cache.is_file():
        raise ValueError(f"Missing linked native cache: {cache}")
    return cache


def audit_flags(text, origin, seen):
    baseline = False
    # Make/Ninja invoke the compiler from the build root, not flags.make's directory.
    # Shell quotes may surround the whole @argument or just its filename.
    pattern = r'''(?<![\w@])(?:["']@([^"']+)["']|@(?:"([^"]+)"|'([^']+)'|([^\s"';]+)))'''
    matches = list(re.finditer(pattern, text))
    for at in re.finditer(r'(?<![\w@])@', text):
        if not any(match.start() <= at.start() < match.end() for match in matches):
            raise ValueError(f"{origin}: unsupported response file syntax")
    for match in matches:
        name = next(group for group in match.groups() if group is not None)
        response = (seen[0] / name).resolve()
        if not response.is_file() or response in seen[1]:
            raise ValueError(f"{origin}: unresolved or recursive response file @{name}")
        seen[1].add(response)
        baseline |= audit_flags(response.read_text(), response, seen)
        seen[1].remove(response)
    # Clang's frontend features may bypass CMake GGML options and -m switches.
    for match in re.finditer(r'(?:-Xclang\s+)?-target-feature\s+(?:-Xclang\s+)?([+-][\w.-]+)', text):
        feature = match.group(1).lower()
        if (feature.startswith('+') and feature[1:] not in REQUIRED_FEATURES | {'sse', 'sse2'}
                or feature.startswith('-') and feature[1:] in REQUIRED_FEATURES):
            raise ValueError(f"{origin}: unsupported CPU flag {match.group(0)}")
    stripped = re.sub(r'(?:-Xclang\s+)?-target-feature\s+(?:-Xclang\s+)?[+-][\w.-]+', '', text)
    if re.search(r'(?<![\w-])(?:-Xclang|-target-feature|-mattr=|-target-cpu=|/clang:)', stripped):
        raise ValueError(f"{origin}: unsupported compiler feature arguments")
    for match in CPU_SWITCH.finditer(text):
        flag = match.group(0).lower()
        if flag.startswith('-m') and not flag.startswith(('-march=', '-mcpu=')):
            if flag[2:] in ALLOWED_M or flag.startswith('-mtune='):
                continue
        elif flag == '-march=x86-64':
            baseline = True
            continue
        elif flag == '/arch:avx2':
            # MSVC spells the same baseline switch differently; ggml emits it for GGML_AVX2.
            baseline = True
            continue
        raise ValueError(f"{origin}: unsupported CPU flag {flag}")
    return baseline


def audit_cache(cache, windows=False):
    lines = cache.read_text().splitlines()
    values = dict(line.split(":BOOL=", 1) for line in lines if ":BOOL=" in line)
    for name in BASELINE_ON:
        if values.get(name) != "ON":
            raise ValueError(f"{cache}: {name} must be ON, got {values.get(name)!r}")
    disabled = set(BASELINE_OFF) | {name for name in values if name.startswith(("GGML_AVX512", "GGML_AMX"))}
    for name in sorted(disabled):
        if values.get(name) != "OFF":
            raise ValueError(f"{cache}: {name} must be OFF, got {values.get(name)!r}")
    files = [p for pattern in ('flags.make', 'compile_commands.json', 'build.ninja')
             for p in cache.parent.rglob(pattern)]
    if not files:
        raise ValueError(f"{cache}: no generated compiler flags to audit (Windows baseline unverified)")
    seen = (cache.parent, set())
    baseline = False
    for file in files:
        if file.name == 'compile_commands.json':
            entries = json.loads(file.read_text())
            if not isinstance(entries, list) or not entries:
                raise ValueError(f"{file}: missing compiler invocations")
            for entry in entries:
                directory = entry.get('directory')
                if not isinstance(directory, str) or not pathlib.Path(directory).is_absolute():
                    raise ValueError(f"{file}: ambiguous compiler working directory")
                cwd = pathlib.Path(directory).resolve()
                if not cwd.is_dir():
                    raise ValueError(f"{file}: unresolved compiler working directory {directory}")
                command = entry.get('command')
                arguments = entry.get('arguments')
                if command is not None and arguments is not None:
                    raise ValueError(f"{file}: ambiguous compiler arguments")
                if isinstance(command, str):
                    baseline |= audit_flags(command, file, (cwd, set()))
                elif isinstance(arguments, list) and arguments and all(isinstance(arg, str) for arg in arguments):
                    baseline |= audit_flags(' '.join(shlex.quote(arg) for arg in arguments), file, (cwd, set()))
                else:
                    raise ValueError(f"{file}: missing compiler arguments")
        else:
            baseline |= audit_flags(file.read_text(), file, seen)
    for line in lines:
        if re.match(r'CMAKE_(?:C|CXX)_FLAGS(?:_[A-Z_]+)?:STRING=', line):
            baseline |= audit_flags(line.split('=', 1)[1], cache, seen)
    if not baseline:
        raise ValueError(f"{cache}: compiler flags must contain -march=x86-64 or /arch:AVX2")
    return len(files)


def audit(target, artifact=None):
    target = target.resolve()
    artifact = artifact or target / ("murmur.exe" if sys.platform == "win32" else "murmur")
    cache = linked_cache(target, artifact)
    count = audit_cache(cache, windows=artifact.suffix.lower() == ".exe")
    print(f"CPU contract baseline Rust + AVX2 llama.cpp: linked {cache} ({count} compiler flag files) clean")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=pathlib.Path)
    parser.add_argument("--artifact", type=pathlib.Path)
    args = parser.parse_args()
    try:
        audit(args.target, args.artifact)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(1, f"CPU audit failed: {exc}\n")
