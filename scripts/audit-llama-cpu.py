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


def audit_cache(cache, windows=False):
    values = dict(line.split(":BOOL=", 1) for line in cache.read_text().splitlines() if ":BOOL=" in line)
    for name in BASELINE_OFF:
        if values.get(name) != "OFF":
            raise ValueError(f"{cache}: {name} must be OFF, got {values.get(name)!r}")
    files = list(cache.parent.rglob("flags.make"))
    if not files:
        files = list(cache.parent.rglob("compile_commands.json"))
    if not files:
        files = list(cache.parent.rglob("build.ninja"))
    if not files:
        raise ValueError(f"{cache}: no generated compiler flags to audit (Windows baseline unverified)")
    for file in files:
        for match in CPU_SWITCH.finditer(file.read_text(errors="replace")):
            flag = match.group(0).lower()
            if flag.startswith("-m") and not flag.startswith(("-march=", "-mcpu=")):
                if flag[2:] in ALLOWED_M or flag.startswith("-mtune="):
                    continue
            elif flag == "-march=x86-64" or flag == "/arch:sse2":
                continue
            raise ValueError(f"{file}: unsupported CPU flag {flag}")
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
