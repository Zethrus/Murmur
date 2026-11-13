#!/usr/bin/env python3
"""Verify the build handed over by windows-build.yml before anything from it runs.

  handoff.py --sha <sha> --dispatched <github.sha> --source C:\\murmur-ci-out\\<sha> --dest <dir> --repo <owner/repo>

The hand-off folder is written by the unprivileged murmur-ci account; the desktop job runs as
the owner. So the job only accepts the commit it was dispatched on, copies the files into the
owner's temp folder, hashes the copies and requires them to equal both SHA256SUMS and the
MURMUR-HANDOFF-SHA256 lines that the build printed in its own job log (fetched from GitHub with
`gh`, so a tampered folder cannot also rewrite the evidence). Writes <dest>/build.json.
The parsing and comparison are pure Python and unit-tested on Linux.
"""
import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

SUMS = "SHA256SUMS"
MARKER = re.compile(r"MURMUR-HANDOFF-SHA256 ([0-9a-f]{40}) ([0-9a-f]{64}) ([A-Za-z0-9._-]+)\s*$")
SUM_LINE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9._-]+)$")
SHA = re.compile(r"^[0-9a-f]{40}$")
# Exactly one file per pattern (windows-build.yml, "Hand the build to the desktop runner").
REQUIRED = (
    "Murmur_*_x64-setup.exe",
    "murmur-windows-x86_64.zip",
    "murmur-engine-windows-x86_64-*.zip",
    "murmur-cuda-windows-x86_64-*.zip",
    "components-manifest.json",
    "components-manifest.json.sig",
    "murmur-release.asc",
    "components-windows-inputs.json",
)
WORKFLOW = "windows-build.yml"


class HandoffError(Exception):
    pass


def check_sha(sha, dispatched):
    """The tested build must be the commit this workflow run was dispatched on."""
    sha, dispatched = (sha or "").strip().lower(), (dispatched or "").strip().lower()
    if not SHA.match(sha):
        raise HandoffError(f"sha must be a full 40-character commit hash, got {sha!r}")
    if sha != dispatched:
        raise HandoffError(f"sha {sha} is not the dispatched commit {dispatched}; dispatch windows-desktop.yml "
                           "on the commit you want to test (the build of that commit must exist)")
    return sha


def parse_sums(text):
    """name -> sha256 from SHA256SUMS; strict format, no paths, no duplicates."""
    sums = {}
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        if not line.strip():
            continue
        match = SUM_LINE.match(line)
        if not match:
            raise HandoffError(f"{SUMS} line {number} is malformed: {line[:120]!r}")
        digest, name = match.groups()
        if name in sums:
            raise HandoffError(f"{SUMS} lists {name} twice")
        sums[name] = digest
    if not sums:
        raise HandoffError(f"{SUMS} is empty")
    return sums


def parse_log(text, commit):
    """name -> sha256 from the MURMUR-HANDOFF-SHA256 lines of `commit` in a job log.
    `gh run view --log` prefixes each line with job, step and timestamp; those are ignored."""
    found = {}
    for line in text.splitlines():
        match = MARKER.search(line)
        if not match or match.group(1) != commit:
            continue
        _, digest, name = match.groups()
        if found.get(name, digest) != digest:
            raise HandoffError(f"The build log lists two different hashes for {name}")
        found[name] = digest
    return found


def check_required(names):
    for pattern in REQUIRED:
        matches = [n for n in names if fnmatch.fnmatchcase(n, pattern)]
        if len(matches) != 1:
            raise HandoffError(f"Expected exactly one {pattern} in the hand-off, found {matches or 'none'}")
    extra = [n for n in names if not any(fnmatch.fnmatchcase(n, p) for p in REQUIRED)]
    if extra:
        raise HandoffError(f"Unexpected files in the hand-off: {', '.join(sorted(extra))}")


def compare(sums, logged):
    """SHA256SUMS and the build log must list the same files with the same hashes."""
    if not logged:
        raise HandoffError("The build log has no MURMUR-HANDOFF-SHA256 lines for this commit")
    if set(sums) != set(logged):
        raise HandoffError(f"SHA256SUMS and the build log list different files: "
                           f"{sorted(set(sums) ^ set(logged))}")
    different = sorted(name for name in sums if sums[name] != logged[name])
    if different:
        raise HandoffError(f"SHA256SUMS disagrees with the build log for {', '.join(different)}")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_verified(source, dest, expected):
    """Copy every expected file to `dest`, then hash the copies (what will run) against `expected`."""
    source, dest = Path(source), Path(dest)
    present = sorted(p.name for p in source.iterdir() if p.is_file() and p.name != SUMS)
    if set(present) != set(expected):
        raise HandoffError(f"Hand-off folder content differs from SHA256SUMS: {sorted(set(present) ^ set(expected))}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in sorted(expected):
        shutil.copyfile(source / name, dest / name)
        actual = sha256(dest / name)
        if actual != expected[name]:
            raise HandoffError(f"{name}: SHA-256 {actual} does not match the build ({expected[name]})")
    return {name: expected[name] for name in sorted(expected)}


def gh(*arguments):
    result = subprocess.run(["gh", *arguments], capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=300)
    if result.returncode:
        raise HandoffError(f"gh {' '.join(arguments[:2])} failed: {result.stderr.strip()[-500:]}")
    return result.stdout


def build_runs(repo, sha, run=gh):
    """Successful windows-build.yml runs of `sha`, newest first: [(id, url, event)]."""
    data = json.loads(run("api", f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?head_sha={sha}&status=success"
                                 "&per_page=20"))
    runs = [r for r in data.get("workflow_runs", []) if r.get("head_sha") == sha and r.get("conclusion") == "success"]
    runs.sort(key=lambda r: r.get("run_started_at") or r.get("created_at") or "", reverse=True)
    return [(r["id"], r.get("html_url", ""), r.get("event", "")) for r in runs]


def matching_run(repo, sha, sums, run=gh):
    """The newest successful build run whose log hashes equal SHA256SUMS."""
    runs = build_runs(repo, sha, run)
    if not runs:
        raise HandoffError(f"No successful {WORKFLOW} run for {sha}; dispatch it on this commit first")
    problems = []
    for run_id, url, event in runs:
        logged = parse_log(run("run", "view", str(run_id), "--repo", repo, "--log"), sha)
        try:
            compare(sums, logged)
        except HandoffError as error:
            problems.append(f"run {run_id} ({event}): {error}")
            continue
        return {"id": run_id, "url": url, "event": event}
    raise HandoffError("No build log matches the hand-off folder:\n" + "\n".join(problems))


def verify(sha, dispatched, source, dest, repo, run=gh):
    sha = check_sha(sha, dispatched)
    source = Path(source)
    if not (source / SUMS).is_file():
        raise HandoffError(f"No hand-off for {sha} at {source} (did windows-build.yml run on Edison for this commit?)")
    sums = parse_sums((source / SUMS).read_text(encoding="utf-8"))
    check_required(sums)
    build = matching_run(repo, sha, sums, run)
    files = copy_verified(source, dest, sums)
    record = {"sha": sha, "build_run": build, "files": files}
    (Path(dest) / "build.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sha", required=True)
    parser.add_argument("--dispatched", required=True, help="github.sha of the desktop run")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = parser.parse_args(argv)
    try:
        if not args.repo:
            raise HandoffError("--repo (or GITHUB_REPOSITORY) is required")
        record = verify(args.sha, args.dispatched, args.source, args.dest.resolve(), args.repo)
    except (HandoffError, OSError, ValueError) as error:
        print(f"Build verification failed: {error}", file=sys.stderr)
        return 1
    print(f"Verified {len(record['files'])} files against SHA256SUMS and build run {record['build_run']['url']}")
    for name, digest in record["files"].items():
        print(f"  {digest}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
