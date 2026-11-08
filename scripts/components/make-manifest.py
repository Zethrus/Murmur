#!/usr/bin/env python3
"""Build the unsigned component manifest (schema 1) from built zips and provenance.toml.

Zip components (engine, CUDA add-on) take size, SHA-256 and installed size from the files in
--components; file components (speech and Enhance models) take pinned files from
provenance.toml. Entries marked `optional_platform` (Windows) are skipped unless
--windows-inputs is given: then the Windows zips must be present and match the hashes the
Windows build recorded (components-windows-inputs.json). Each entry may name its own
`lock` (hash-locked requirements) and `cuda_lock`. Sign the output with sign-manifest.sh.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = 1


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def rfc3339(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lock_entry(lock, package):
    match = re.search(rf"(?m)^{re.escape(package)}==([\w.]+) --hash=sha256:([a-f0-9]{{64}})$", lock.read_text())
    if not match:
        raise SystemExit(f"{lock}: no pinned {package}")
    return match.groups()


def git_revision():
    result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def check_windows_inputs(inputs, components_dir, version):
    """The Windows zips present here are byte-for-byte what the Windows build recorded."""
    if inputs.get("version") != version or inputs.get("os") != "windows":
        raise SystemExit("Windows manifest input is for another version or platform")
    for kind in ("engine", "cuda"):
        entry = inputs.get("components", {}).get(kind) or {}
        path = components_dir / entry.get("zip", "")
        if not entry.get("zip") or "/" in entry["zip"] or not path.is_file():
            raise SystemExit(f"Windows {kind} zip missing: {entry.get('zip')}")
        if path.stat().st_size != entry.get("size_bytes") or sha256(path) != entry.get("sha256"):
            raise SystemExit(f"{path.name} differs from the Windows build's manifest input")


def build(provenance, components_dir, version, base_url, serial, issued, expires, lock, windows_inputs=None,
          systems=("linux", "windows")):
    """`systems` limits platform-specific entries (a Windows-only test manifest); `any` stays."""
    entries = []
    if windows_inputs is not None:
        check_windows_inputs(windows_inputs, components_dir, version)
    for key, spec in provenance["components"].items():
        if spec["os"] != "all" and spec["os"] not in systems:
            continue
        component_id = spec.get("id", key)
        base = {
            "id": component_id, "version": spec.get("version", version), "kind": spec["kind"],
            "os": "any" if spec["os"] == "all" else spec["os"], "arch": spec["arch"], "archive": spec["archive"],
            "depends": spec.get("depends", []), "license": spec["license"],
            "display_name": spec["display_name"], "description": spec["description"],
        }
        provenance_entry = {"type": spec["provenance"]["type"], "source_url": spec["provenance"]["source_url"],
                            "source_revision": spec["provenance"].get("source_revision", ""),
                            "source_sha256": "", "note": spec["provenance"].get("note", "")}
        for optional in ("model", "quality", "speed", "memory_mb", "languages"):
            if optional in spec:
                base[optional] = spec[optional]
        if spec["archive"] == "zip":
            name = spec["zip"].format(version=version)
            path = components_dir / name
            if spec.get("optional_platform") and windows_inputs is None:
                # Only zips the Windows build vouched for (by hash) are ever signed.
                print(f"skipping {key}: Windows not part of this manifest", file=sys.stderr)
                continue
            if not path.is_file():
                raise SystemExit(f"Missing component archive: {path}")
            with zipfile.ZipFile(path) as archive:
                installed = sum(info.file_size for info in archive.infolist())
            base.update(urls=[base_url + name], size_bytes=path.stat().st_size, sha256=sha256(path),
                        installed_size_bytes=installed, temp_size_bytes=0)
            entry_lock = ROOT / spec["lock"] if "lock" in spec else lock
            if spec["kind"] == "engine":
                provenance_entry.update(source_revision=git_revision(), source_sha256=sha256(entry_lock))
            elif spec["kind"] == "cuda":
                cublas_version, wheel = lock_entry(entry_lock, "nvidia-cublas-cu12")
                provenance_entry.update(source_revision=f"nvidia-cublas-cu12=={cublas_version}", source_sha256=wheel)
        else:
            files = [{"name": f["name"], "urls": [f["url"]], "size_bytes": f["size_bytes"], "sha256": f["sha256"]}
                     for f in spec["files"]]
            total = sum(f["size_bytes"] for f in files)
            largest = max(files, key=lambda f: f["size_bytes"])
            provenance_entry["source_sha256"] = largest["sha256"]
            base.update(files=files, size_bytes=total, installed_size_bytes=total, temp_size_bytes=0)
        base["provenance"] = provenance_entry
        entries.append(base)
    return {"schema_version": SCHEMA, "serial": serial, "issued_at": rfc3339(issued),
            "expires_at": rfc3339(expires), "channel": "stable", "components": entries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--components", type=Path, required=True, help="directory with the built component zips")
    parser.add_argument("--provenance", type=Path, default=Path(__file__).with_name("provenance.toml"))
    parser.add_argument("--version", required=True, help="Murmur version, e.g. 0.5.0")
    parser.add_argument("--base-url", help="release download URL ending in /; default: GitHub release v<version>")
    parser.add_argument("--serial", type=int, help="monotonic serial; default: issue time in Unix seconds")
    parser.add_argument("--valid-days", type=int, default=180)
    parser.add_argument("--lock", type=Path, default=ROOT / "engine/requirements-linux-release.txt")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--os", action="append", choices=("linux", "windows"), dest="systems",
                        help="platforms to list (repeatable); default both")
    parser.add_argument("--windows-inputs", type=Path,
                        help="components-windows-inputs.json from windows-build.yml; Windows entries become required")
    args = parser.parse_args()
    issued = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    base = args.base_url or f"https://github.com/Zethrus/Murmur/releases/download/v{args.version}/"
    if not base.startswith("https://github.com/") or not base.endswith("/"):
        parser.error("--base-url must be an https://github.com/ release URL ending in /")
    manifest = build(tomllib.loads(args.provenance.read_text()), args.components, args.version, base,
                     args.serial or int(issued.timestamp()), issued,
                     issued + datetime.timedelta(days=args.valid_days), args.lock,
                     json.loads(args.windows_inputs.read_text()) if args.windows_inputs else None,
                     tuple(args.systems or ("linux", "windows")))
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"{args.output}: serial {manifest['serial']}, {len(manifest['components'])} components")


if __name__ == "__main__":
    main()
