#!/usr/bin/env python3
"""Pin Hugging Face model files for the component manifest (maintainer tool, needs network).

For each repository: resolve a commit, list the inference files at that commit, and record
size and SHA-256 per file. LFS files take the SHA-256 Hugging Face publishes for the blob;
small (non-LFS) files are downloaded at the pinned commit and hashed here. Output is TOML for
scripts/components/provenance.toml. Nothing is trusted from a branch name after this runs.
"""
import argparse
import hashlib
import json
import sys
import urllib.request

SPEECH_FILES = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt",
                "vocabulary.json", "preprocessor_config.json")


def get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "murmur-pin-models"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def pin(repo, revision, wanted):
    info = json.loads(get(f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true"))
    commit = info["sha"]
    files = []
    for sibling in info["siblings"]:
        name = sibling["rfilename"]
        if name not in wanted:
            continue
        lfs = sibling.get("lfs")
        url = f"https://huggingface.co/{repo}/resolve/{commit}/{name}"
        if lfs:
            size, digest = lfs["size"], lfs["sha256"]
        else:
            data = get(url)
            size, digest = len(data), hashlib.sha256(data).hexdigest()
            if size != sibling["size"]:
                raise SystemExit(f"{repo}/{name}: size changed while pinning")
        files.append({"name": name, "url": url, "size_bytes": size, "sha256": digest})
    return commit, sorted(files, key=lambda f: f["name"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("entries", nargs="+", help="ID=REPO[@REVISION][:file,file] e.g. speech-tiny.en=Systran/faster-whisper-tiny.en")
    args = parser.parse_args()
    for entry in args.entries:
        component, spec = entry.split("=", 1)
        spec, _, names = spec.partition(":")
        repo, _, revision = spec.partition("@")
        wanted = tuple(names.split(",")) if names else SPEECH_FILES
        commit, files = pin(repo, revision or "main", wanted)
        print(f'[components."{component}"]\nsource_url = "https://huggingface.co/{repo}"\nsource_revision = "{commit}"')
        for f in files:
            print(f'[[components."{component}".files]]\nname = "{f["name"]}"\nurl = "{f["url"]}"\n'
                  f'size_bytes = {f["size_bytes"]}\nsha256 = "{f["sha256"]}"')
        print()
        sys.stdout.flush()


if __name__ == "__main__":
    main()
