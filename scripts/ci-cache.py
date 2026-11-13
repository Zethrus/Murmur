#!/usr/bin/env python3
"""Prepare and measure build caches without changing the checked-out source tree.

The self-hosted cache is shared only by jobs serialized in the workflow. This script
never prunes another repository, runner, platform or active toolchain namespace.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

RUST_VERSION = "1.100.0"
MAX_TARGET_BYTES = 30 * 1024**3
MAX_DOWNLOAD_BYTES = 8 * 1024**3
MAX_NAMESPACE_BYTES = 40 * 1024**3
OLD_TOOLCHAIN_DAYS = 14


def is_redirect(path):
    # pathlib identifies Windows junctions separately from symbolic links.
    return path.is_symlink() or path.is_junction()


def namespace(value):
    label = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")[:32] or "unnamed"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{label}-{digest}"


def size(path):
    if not path.exists() or is_redirect(path):
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for folder, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not is_redirect(Path(folder) / name)]
        for name in files:
            entry = Path(folder) / name
            if not is_redirect(entry):
                try:
                    total += entry.stat().st_size
                except FileNotFoundError:
                    pass
    return total


def prune(path, limit, reason):
    if is_redirect(path):
        raise ValueError(f"Refusing symlink/junction cache: {path}")
    if path.exists() and size(path) > limit:
        print(f"Pruning {reason}: {path} exceeds {limit / 1024**3:.0f} GiB")
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def cache_paths(env):
    workspace = Path(env["GITHUB_WORKSPACE"]).resolve()
    if env.get("RUNNER_ENVIRONMENT") != "self-hosted":
        return {"target": workspace / "src-tauri" / "target",
                "pip": workspace / ".cache" / "pip",
                "npm": workspace / ".cache" / "npm",
                "pyinstaller": workspace / ".cache" / "pyinstaller",
                "cargo": workspace / ".cache" / "cargo",
                "rustup": workspace / ".cache" / "rustup",
                "root": workspace / ".cache"}
    tool_cache = Path(env["RUNNER_TOOL_CACHE"]).resolve()
    scope = (tool_cache / "murmur-build-cache" /
             namespace(env["GITHUB_REPOSITORY"]) /
             namespace(env["RUNNER_NAME"]) /
             namespace(f'{env["RUNNER_OS"]}-{env["RUNNER_ARCH"]}'))
    root = scope / f"rust-{RUST_VERSION}"
    if workspace == root or workspace in root.parents:
        raise ValueError("Runner-local cache must be outside GITHUB_WORKSPACE")
    return {key: root / key for key in ("target", "pip", "npm", "pyinstaller", "cargo", "rustup")} | {"root": root}


def prepare(env):
    paths = cache_paths(env)
    root = paths["root"]
    if env.get("RUNNER_ENVIRONMENT") == "self-hosted":
        for ancestor in (root, root.parent, root.parent.parent,
                         root.parent.parent.parent, root.parent.parent.parent.parent):
            if is_redirect(ancestor):
                raise ValueError(f"Refusing symlink/junction cache: {ancestor}")
        # cargo/registry and cargo/git are pruned below. A junction or symlink at
        # cargo itself redirects both deletions outside this namespace.
        for path in paths.values():
            if is_redirect(path):
                raise ValueError(f"Refusing symlink/junction cache: {path}")
        root.parent.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - OLD_TOOLCHAIN_DAYS * 86400
        for old in root.parent.iterdir():
            if old == root or not re.fullmatch(r"rust-\d+\.\d+\.\d+", old.name):
                continue
            marker = old / ".last-used"
            if is_redirect(old):
                raise ValueError(f"Refusing symlink/junction cache: {old}")
            if old.is_dir() and (marker.stat().st_mtime if marker.exists() else old.stat().st_mtime) < cutoff:
                print(f"Pruning toolchain cache unused for {OLD_TOOLCHAIN_DAYS} days: {old}")
                shutil.rmtree(old)
        prune(paths["target"], MAX_TARGET_BYTES, "Cargo target")
        for key in ("pip", "npm", "pyinstaller"):
            prune(paths[key], MAX_DOWNLOAD_BYTES, key)
        # Keep cargo/bin: it contains rustup shims. Only downloaded crates and git
        # sources are disposable when their cache exceeds the bound.
        for source in ("registry", "git"):
            prune(paths["cargo"] / source, MAX_DOWNLOAD_BYTES, f"cargo {source}")
        total = size(root)
        for disposable in (paths["pip"], paths["npm"], paths["pyinstaller"],
                           paths["cargo"] / "registry", paths["cargo"] / "git", paths["target"]):
            if total <= MAX_NAMESPACE_BYTES:
                break
            amount = size(disposable)
            if amount:
                print(f"Pruning {disposable} to keep this cache namespace under "
                      f"{MAX_NAMESPACE_BYTES / 1024**3:.0f} GiB")
                shutil.rmtree(disposable)
                disposable.mkdir(parents=True)
                total -= amount
        if total > MAX_NAMESPACE_BYTES:
            raise ValueError(f"Non-disposable toolchain cache exceeds {MAX_NAMESPACE_BYTES / 1024**3:.0f} GiB")
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    (root / ".last-used").touch()
    temporary = Path(env["GITHUB_WORKSPACE"]) / ".cache" / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    variables = {
        "CARGO_TARGET_DIR": paths["target"], "PIP_CACHE_DIR": paths["pip"],
        "NPM_CONFIG_CACHE": paths["npm"], "PYINSTALLER_CONFIG_DIR": paths["pyinstaller"],
        "CARGO_HOME": paths["cargo"], "RUSTUP_HOME": paths["rustup"],
    }
    if env.get("RUNNER_OS") == "Windows":
        variables.update(TMP=temporary, TEMP=temporary)
    else:
        variables["TMPDIR"] = temporary
    with open(env["GITHUB_ENV"], "a", encoding="utf-8") as output:
        for key, value in variables.items():
            print(f"{key}={value}", file=output)
    with open(env["GITHUB_PATH"], "a", encoding="utf-8") as output:
        print(paths["cargo"] / "bin", file=output)
    print(f"Cargo target: {paths['target']} ({size(paths['target']) / 1024**3:.2f} GiB at start)")
    print(f"pip download cache: {paths['pip']} ({size(paths['pip']) / 1024**3:.2f} GiB at start)")
    print(f"Cache mode: {'runner-local' if env.get('RUNNER_ENVIRONMENT') == 'self-hosted' else 'hosted'}")


def free_memory():
    if sys.platform == "win32":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total_physical", ctypes.c_ulonglong), ("available_physical", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                        ("available_extended", ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        return status.available_physical
    with open("/proc/meminfo", encoding="ascii") as source:
        for line in source:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    raise OSError("MemAvailable missing")


def snapshot(env, label):
    paths = cache_paths(env)
    now = time.time()
    record = Path(env["RUNNER_TEMP"]) / f"murmur-ci-evidence-{env['GITHUB_RUN_ID']}-{env.get('GITHUB_RUN_ATTEMPT', '1')}.json"
    if label == "start":
        record.write_text(json.dumps({"started": now}), encoding="utf-8")
    started = json.loads(record.read_text(encoding="utf-8"))["started"] if record.exists() else now
    free = shutil.disk_usage(paths["root"]).free
    lines = [f"### Build cache evidence ({label})",
             f"- Runner: {env.get('RUNNER_NAME', 'unknown')} / {env.get('RUNNER_OS', 'unknown')} / {env.get('RUNNER_ARCH', 'unknown')}",
             f"- Rust: {RUST_VERSION}; target: `{paths['target']}`",
             f"- Elapsed since start: {(now - started) / 60:.1f} min",
             f"- Free disk: {free / 1024**3:.2f} GiB; available RAM: {free_memory() / 1024**3:.2f} GiB"]
    for key in ("target", "pip", "npm", "cargo"):
        lines.append(f"- {key} cache: {size(paths[key]) / 1024**3:.2f} GiB")
    message = "\n".join(lines) + "\n"
    print(message)
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write(message + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "start", "finish"))
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(os.environ)
        else:
            snapshot(os.environ, args.command)
    except (KeyError, OSError, ValueError) as error:
        parser.exit(1, f"CI cache error: {error}\n")


if __name__ == "__main__":
    main()
