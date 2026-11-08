#!/usr/bin/env python3
"""Audit the llama.cpp native build actually referenced by a Cargo binary.

Cargo fingerprint dependency hashes identify the precise sys crate and build-script output.
Timestamps are used in one place only: windows-msvc binaries carry no metadata hash, so their
fingerprint record is the one Cargo wrote between linking and the artifact's last write.
Run before stripping or packaging the binary.
"""
import argparse
import hashlib
import json
import pathlib
import re
import shlex
import sys

BASELINE_ON = ("GGML_AVX", "GGML_AVX2", "GGML_FMA", "GGML_F16C", "GGML_BMI2", "GGML_SSE42")
# Options ggml's CMakeLists only declares `if (NOT MSVC)`.
MSVC_IMPLIED = ("GGML_FMA", "GGML_F16C")
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


def _linked_at(binary):
    """When the linker wrote `binary`. On Windows target/<profile>/murmur.exe is a hard link to
    deps/murmur.exe, so tauri's in-place bundle-type patch moves the modification time of both;
    the creation time stays at (or before) the link."""
    info = binary.stat()
    if sys.platform == "win32":
        return getattr(info, "st_birthtime", info.st_ctime)
    return info.st_mtime


def _newest_bin_fingerprint(fp, binary, slack=2):
    """Hash of the `murmur` binary fingerprint Cargo wrote for an unhashed deps binary.

    Cargo writes a binary's fingerprint right after linking it, and tauri's bundle-type patch
    (the artifact's last write) comes later still. So the record must fall between the link and
    the artifact's modification time: cache-restored records are older, and a later `cargo
    check`/`clippy` of the same profile is newer. Exactly one record must qualify."""
    linked, written = _linked_at(binary), binary.stat().st_mtime
    records = [path for path in fp.glob("murmur-*/bin-murmur")
               if linked - slack <= path.stat().st_mtime <= written + slack]
    if len(records) != 1:
        raise ValueError(f"Expected one Cargo fingerprint written with {binary}, found {len(records)}")
    return records[0].parent.name.split("-", 1)[1]


def linked_cache(target, artifact):
    target = target.resolve()
    artifact = artifact.resolve()
    deps = target / "deps"
    if artifact == target / ("murmur.exe" if sys.platform == "win32" else "murmur"):
        digest = _unpatched_digest(artifact)
        candidates = [p for p in deps.glob("murmur-*") if p.is_file() and p.suffix not in (".d", ".pdb")]
        # windows-msvc: Cargo omits the metadata hash from executable names (so the .pdb
        # name stays stable), leaving deps/murmur.exe; its fingerprint is found by time.
        unhashed = deps / artifact.name
        if not candidates and unhashed.is_file():
            candidates = [unhashed]
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
    fp = target / ".fingerprint"
    if artifact.parent == deps and artifact.stem == "murmur":
        binary_hash = _newest_bin_fingerprint(fp, artifact)
    elif artifact.parent != deps or not re.fullmatch(r"murmur-[0-9a-f]{16}(?:\.exe)?", artifact.name):
        raise ValueError("Artifact must be a current unstripped Cargo binary or target/<profile>/murmur")
    else:
        binary_hash = artifact.stem.split("-", 1)[1]
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
    unsupported = []
    for match in CPU_SWITCH.finditer(text):
        # Upper-case -M switches are never CPU flags: GCC/Clang dependency output (-MD, -MT,
        # -MF) and MSVC runtime selection (-MD, -MT, -MDd); CPU switches are lower-case -m.
        if match.group(0).startswith("-M"):
            continue
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
        if flag not in unsupported:
            unsupported.append(flag)
    if unsupported:
        # Every offending flag at once: a Windows CI round trip costs most of an hour.
        raise ValueError(f"{origin}: unsupported CPU flag {', '.join(unsupported)}")
    return baseline


def audit_cache(cache, windows=False):
    lines = cache.read_text().splitlines()
    values = dict(line.split(":BOOL=", 1) for line in lines if ":BOOL=" in line)
    for name in BASELINE_ON:
        # ggml does not define FMA/F16C for MSVC: /arch:AVX2 (checked below) implies them.
        if windows and name in MSVC_IMPLIED and name not in values:
            continue
        if values.get(name) != "ON":
            raise ValueError(f"{cache}: {name} must be ON, got {values.get(name)!r}")
    disabled = set(BASELINE_OFF) | {name for name in values if name.startswith(("GGML_AVX512", "GGML_AMX"))}
    for name in sorted(disabled):
        # Nor the AMX options (MSVC cannot build AMX); absent there means not built.
        if windows and name.startswith("GGML_AMX") and name not in values:
            continue
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
    # FMA/F16C were accepted as absent above only because MSVC's /arch:AVX2 implies them;
    # -march=x86-64 implies neither, so the MSVC switch itself must be present.
    if windows and any(name not in values for name in MSVC_IMPLIED):
        texts = [file.read_text(errors="replace") for file in files]
        texts += [line for line in lines if line.startswith(("CMAKE_C_FLAGS", "CMAKE_CXX_FLAGS"))]
        if not any("/arch:avx2" in text.lower() for text in texts):
            raise ValueError(f"{cache}: GGML_FMA/F16C are implied only by /arch:AVX2, which is missing")
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
