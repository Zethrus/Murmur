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

BASELINE_OFF = (
    "GGML_NATIVE", "GGML_AVX", "GGML_AVX2", "GGML_AVX512", "GGML_AVX512_BF16",
    "GGML_AVX512_VBMI", "GGML_AVX512_VNNI", "GGML_AVX_VNNI", "GGML_FMA",
    "GGML_F16C", "GGML_BMI2", "GGML_SSE42", "GGML_CPU_ALL_VARIANTS",
    "GGML_AMX_BF16", "GGML_AMX_INT8", "GGML_AMX_TILE",
)
# x86-64-v2, SSE3/SSSE3/SSE4, POPCNT, AES, BMI, AVX and native tuning all
# raise the advertised SSE2 minimum. Reject unknown -m CPU switches too.
ALLOWED_M = {"64", "sse", "sse2", "no-avx", "no-avx2", "no-fma", "no-f16c", "no-bmi2", "no-sse4.2", "no-native"}
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


def linked_cache(target, artifact):
    target = target.resolve()
    artifact = artifact.resolve()
    deps = target / "deps"
    if artifact == target / ("murmur.exe" if sys.platform == "win32" else "murmur"):
        digest = hashlib.sha256(artifact.read_bytes()).digest()
        binaries = [p for p in deps.glob("murmur-*") if p.is_file() and p.suffix not in (".d", ".pdb")
                    and p.stat().st_size == artifact.stat().st_size
                    and hashlib.sha256(p.read_bytes()).digest() == digest]
        if len(binaries) != 1:
            raise ValueError(f"Cannot uniquely match {artifact} to a Cargo binary (stripped or stale artifact?)")
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
        audit_flags(response.read_text(), response, seen)
        seen[1].remove(response)
    # Clang's frontend features may bypass CMake GGML options and -m switches.
    for match in re.finditer(r'(?:-Xclang\s+)?-target-feature\s+(?:-Xclang\s+)?([+-][\w.-]+)', text):
        if match.group(1).lower() not in ('+sse', '+sse2') and not match.group(1).startswith('-'):
            raise ValueError(f"{origin}: unsupported CPU flag {match.group(0)}")
    stripped = re.sub(r'(?:-Xclang\s+)?-target-feature\s+(?:-Xclang\s+)?[+-][\w.-]+', '', text)
    if re.search(r'(?<![\w-])(?:-Xclang|-target-feature|-mattr=|-target-cpu=|/clang:)', stripped):
        raise ValueError(f"{origin}: unsupported compiler feature arguments")
    for match in CPU_SWITCH.finditer(text):
        flag = match.group(0).lower()
        if flag.startswith('-m') and not flag.startswith(('-march=', '-mcpu=')):
            if flag[2:] in ALLOWED_M or flag.startswith('-mtune='):
                continue
        elif flag == '-march=x86-64' or flag == '/arch:sse2':
            continue
        raise ValueError(f"{origin}: unsupported CPU flag {flag}")


def audit_cache(cache, windows=False):
    lines = cache.read_text().splitlines()
    values = dict(line.split(":BOOL=", 1) for line in lines if ":BOOL=" in line)
    for name in BASELINE_OFF:
        if values.get(name) != "OFF":
            raise ValueError(f"{cache}: {name} must be OFF, got {values.get(name)!r}")
    files = [p for pattern in ('flags.make', 'compile_commands.json', 'build.ninja')
             for p in cache.parent.rglob(pattern)]
    if not files:
        raise ValueError(f"{cache}: no generated compiler flags to audit (Windows baseline unverified)")
    seen = (cache.parent, set())
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
                    audit_flags(command, file, (cwd, set()))
                elif isinstance(arguments, list) and arguments and all(isinstance(arg, str) for arg in arguments):
                    audit_flags(' '.join(shlex.quote(arg) for arg in arguments), file, (cwd, set()))
                else:
                    raise ValueError(f"{file}: missing compiler arguments")
        else:
            audit_flags(file.read_text(), file, seen)
    for line in lines:
        if re.match(r'CMAKE_(?:C|CXX)_FLAGS(?:_[A-Z_]+)?:STRING=', line):
            audit_flags(line.split('=', 1)[1], cache, seen)
    return len(files)


def audit(target, artifact=None):
    target = target.resolve()
    artifact = artifact or target / ("murmur.exe" if sys.platform == "win32" else "murmur")
    cache = linked_cache(target, artifact)
    count = audit_cache(cache, windows=artifact.suffix.lower() == ".exe")
    print(f"CPU baseline x86-64/SSE2: linked {cache} ({count} compiler flag files) clean")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=pathlib.Path)
    parser.add_argument("--artifact", type=pathlib.Path)
    args = parser.parse_args()
    try:
        audit(args.target, args.artifact)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(1, f"CPU audit failed: {exc}\n")
