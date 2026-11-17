#!/usr/bin/env python3
"""Stage a pinned speech model for the release GPU tests.

Downloads the model's files from the URLs pinned in scripts/components/provenance.toml into
DEST/<model>/, checking the size and SHA-256 of each one. Files that are already present
with the right hash are kept, so a runner-local folder is filled once. The test then installs
the folder through the app's own verifier against the signed manifest, which checks every
file again: staging never decides what is trusted.
"""
import argparse
import hashlib
import os
from pathlib import Path
import sys
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / "scripts" / "components" / "provenance.toml"
ALLOWED_PREFIX = "https://huggingface.co/"
CHUNK = 1 << 20


def pinned_files(provenance, model):
    spec = provenance.get("components", {}).get(f"speech-{model}")
    if not spec or spec.get("kind") != "speech-model" or spec.get("model") != model:
        raise ValueError(f"No pinned speech model named {model!r}")
    files = spec.get("files") or []
    if not files:
        raise ValueError(f"{model}: no pinned files")
    for entry in files:
        name = entry["name"]
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            raise ValueError(f"{model}: unsafe file name {name!r}")
        if not entry["url"].startswith(ALLOWED_PREFIX):
            raise ValueError(f"{model}: {name} is not pinned to huggingface.co")
        if len(entry["sha256"]) != 64 or int(entry["size_bytes"]) <= 0:
            raise ValueError(f"{model}: {name} has no usable size or SHA-256")
    return files


def digest(path):
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            hasher.update(chunk)
            size += len(chunk)
    return size, hasher.hexdigest()


def matches(path, entry):
    return path.is_file() and not path.is_symlink() and digest(path) == (int(entry["size_bytes"]), entry["sha256"])


def download(url, target, opener=urllib.request.urlopen):
    partial = target.with_name(target.name + ".partial")
    try:
        with opener(url, timeout=60) as response, partial.open("wb") as out:
            while chunk := response.read(CHUNK):
                out.write(chunk)
        return partial
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def stage(model, dest, provenance=None, fetch=download):
    provenance = provenance if provenance is not None else tomllib.loads(PROVENANCE.read_text())
    files = pinned_files(provenance, model)
    folder = Path(dest) / model
    folder.mkdir(parents=True, exist_ok=True)
    fetched = []
    for entry in files:
        target = folder / entry["name"]
        if matches(target, entry):
            continue
        partial = fetch(entry["url"], target)
        if not matches(partial, entry):
            partial.unlink(missing_ok=True)
            raise ValueError(f"{entry['name']}: downloaded file does not match its pinned size and SHA-256")
        os.replace(partial, target)
        fetched.append(entry["name"])
    return folder, fetched


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="distil-small.en")
    parser.add_argument("--dest", type=Path, required=True, help="models folder; the model goes in DEST/<model>")
    args = parser.parse_args(argv)
    try:
        folder, fetched = stage(args.model, args.dest)
    except (OSError, ValueError, KeyError) as error:
        print(f"Model staging failed: {error}", file=sys.stderr)
        return 1
    print(f"{folder}: {'downloaded ' + ', '.join(fetched) if fetched else 'already staged'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
