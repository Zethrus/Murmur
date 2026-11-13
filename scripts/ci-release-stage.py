#!/usr/bin/env python3
"""Stage and verify the four Windows tag assets between jobs on one runner."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil


def asset_paths(source, version):
    return {
        f"Murmur_{version}_x64-setup.exe": source / "windows" / f"Murmur_{version}_x64-setup.exe",
        "murmur-windows-x86_64.zip": source / "windows" / "murmur-windows-x86_64.zip",
        f"murmur-engine-windows-x86_64-{version}.zip": source / "components-windows" / f"murmur-engine-windows-x86_64-{version}.zip",
        f"murmur-cuda-windows-x86_64-{version}.zip": source / "components-windows" / f"murmur-cuda-windows-x86_64-{version}.zip",
    }


def stage_path(tool_cache, run_id, attempt):
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)) or not re.fullmatch(r"[1-9][0-9]*", str(attempt)):
        raise ValueError("Invalid run identity")
    root = Path(tool_cache) / "murmur-release-stage"
    stage = root / f"{run_id}-{attempt}"
    for entry in (Path(tool_cache), root, stage):
        if entry.is_symlink() or entry.is_junction():
            raise ValueError(f"Release stage must not be a symlink or junction: {entry}")
    return stage


def identity(run_id, attempt, sha, version):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid release SHA")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", version):
        raise ValueError("Invalid release version")
    return {"schema": 1, "run_id": str(run_id), "attempt": str(attempt), "sha": sha, "version": version}


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_stage(tool_cache, run_id, attempt, sha, version, source):
    stage = stage_path(tool_cache, run_id, attempt)
    record = identity(run_id, attempt, sha, version)
    files = asset_paths(Path(source), version)
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    assets = {}
    for name, origin in files.items():
        if not origin.is_file() or origin.is_symlink() or origin.is_junction():
            raise ValueError(f"Missing or redirected Windows release asset: {origin}")
        target = stage / name
        shutil.copyfile(origin, target)
        if target.stat().st_size == 0:
            raise ValueError(f"Empty Windows release asset: {name}")
        assets[name] = {"size": target.stat().st_size, "sha256": digest(target)}
    record["assets"] = assets
    (stage / "stage.json.tmp").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    (stage / "stage.json.tmp").replace(stage / "stage.json")
    return stage


def verify_stage(tool_cache, run_id, attempt, sha, version):
    stage = stage_path(tool_cache, run_id, attempt)
    record = json.loads((stage / "stage.json").read_text(encoding="utf-8"))
    expected = identity(run_id, attempt, sha, version)
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("Windows release stage identity mismatch")
    names = set(asset_paths(Path("."), version))
    if set(record.get("assets", {})) != names or {item.name for item in stage.iterdir()} != names | {"stage.json"}:
        raise ValueError("Windows release stage has missing or unexpected assets")
    for name, evidence in record["assets"].items():
        path = stage / name
        if not path.is_file() or path.is_symlink() or path.is_junction():
            raise ValueError(f"Missing or redirected staged asset: {name}")
        if path.stat().st_size == 0 or path.stat().st_size != evidence["size"] or digest(path) != evidence["sha256"]:
            raise ValueError(f"Windows release stage hash or size mismatch: {name}")
    return stage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("stage", "verify"))
    parser.add_argument("--tool-cache", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source", default=".cache/artifacts")
    args = parser.parse_args()
    try:
        if args.command == "stage":
            path = write_stage(args.tool_cache, args.run_id, args.attempt, args.sha, args.version, args.source)
        else:
            path = verify_stage(args.tool_cache, args.run_id, args.attempt, args.sha, args.version)
        print(f"Verified Windows release stage: {path}")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Windows release stage: {error}\n")


if __name__ == "__main__":
    main()
