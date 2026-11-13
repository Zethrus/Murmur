#!/usr/bin/env python3
"""Trusted default-branch controller and read-only build gate for ordinary PR CI.

Only ``control`` needs contents/actions/statuses write. The build jobs call ``gate``
with contents/actions read and never receive the controller token. No code from a
PR checkout is executed by the controller.
"""
import argparse
from datetime import datetime, timezone
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_VERSION = "2022-11-28"
SOURCE = {"linux": "linux-build.yml", "windows": "windows-build.yml"}
CONTEXT = {"linux": "Murmur Linux build result", "windows": "Murmur Windows build result"}
MANUAL_CONTEXT = {"linux": "Murmur Linux manual build result", "windows": "Murmur Windows manual build result"}
SHA = re.compile(r"^[0-9a-f]{40}$")
HOST_TITLE = re.compile(r"^Hosted fallback (linux|windows) (\d+) (\d+) ([0-9a-f]{40})$")
CLAIM_WAIT_SECONDS = 420  # bounded; a delayed controller fails the build visibly
DISPATCH_RETRY_MINUTES = 3
MAX_DISPATCH_ATTEMPTS = 3
HOST_ALLOCATION_MINUTES = 30


class ApiError(RuntimeError):
    def __init__(self, status, path, body):
        super().__init__(f"GitHub API {status} at {path}: {body[:400]}")
        self.status = status


class GitHub:
    def __init__(self, repo, token, base=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("Invalid GITHUB_REPOSITORY")
        self.repo = repo
        self.base = (base or "https://api.github.com").rstrip("/") + "/repos/" + repo
        self.token = token

    def request(self, method, path, data=None, expected=(200,)):
        url = self.base + path
        payload = None if data is None else json.dumps(data).encode()
        request = Request(url, data=payload, method=method,
                          headers={"Accept": "application/vnd.github+json",
                                   "Authorization": "Bearer " + self.token,
                                   "X-GitHub-Api-Version": API_VERSION,
                                   "User-Agent": "murmur-ci-build-controller",
                                   **({"Content-Type": "application/json"} if payload else {})})
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read()
                if response.status not in expected:
                    raise ApiError(response.status, path, body.decode(errors="replace"))
                return json.loads(body) if body else None
        except HTTPError as error:
            body = error.read().decode(errors="replace")
            if error.code == 404 and 404 in expected:
                return None
            raise ApiError(error.code, path, body) from error
        except URLError as error:
            raise RuntimeError(f"GitHub API unavailable at {path}: {error}") from error

    def get(self, path, missing=False):
        for attempt in range(2):
            try:
                return self.request("GET", path, expected=(200, 404) if missing else (200,))
            except IncompleteRead as error:
                if attempt:
                    raise RuntimeError(f"Incomplete GitHub API response at {path}: {error}") from error
                print(f"Retrying incomplete read-only API response at {path}")

    def post(self, path, data, expected=(201,)):
        return self.request("POST", path, data, expected=expected)


def age_minutes(timestamp, now):
    return (now - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds() / 60


def ref_name(kind, platform, run_id, attempt):
    return f"ci-build-{kind}/v1/{platform}/{run_id}-{attempt}"


def read_marker(api, kind, platform, run_id, attempt):
    name = ref_name(kind, platform, run_id, attempt)
    ref = api.get("/git/ref/tags/" + name, missing=True)
    if ref is None:
        return None
    obj = ref.get("object", {})
    if obj.get("type") != "tag" or not SHA.fullmatch(obj.get("sha", "")):
        raise ValueError(f"Malformed immutable marker {name}")
    tag = api.get("/git/tags/" + obj["sha"])
    marker = json.loads(tag["message"])
    if tag.get("tag") != name or marker.get("platform") != platform or marker.get("original_run") != run_id or marker.get("original_attempt") != attempt:
        raise ValueError(f"Marker identity mismatch: {name}")
    return marker


def create_marker(api, kind, platform, run_id, attempt, sha, **fields):
    name = ref_name(kind, platform, run_id, attempt)
    marker = {"platform": platform, "original_run": run_id,
              "original_attempt": attempt, "sha": sha,
              "created_at": datetime.now(timezone.utc).isoformat(), **fields}
    tag = api.post("/git/tags", {"tag": name, "message": json.dumps(marker, sort_keys=True),
                                  "object": sha, "type": "commit"})
    try:
        api.post("/git/refs", {"ref": "refs/tags/" + name, "sha": tag["sha"]})
        print(f"Created {kind} marker {name}: {marker}")
        return marker, True
    except ApiError as error:
        if error.status != 422:
            raise
        # 422 may mean abuse/validation, not an existing ref. Read back and
        # validate the exact ref; any other result is a hard API failure.
        existing = read_marker(api, kind, platform, run_id, attempt)
        if existing is None:
            raise
        print(f"Marker {name} already exists: {existing}")
        return existing, False


def source_sha(run, platform):
    title = run.get("display_title", "")
    suffix = r" (branch|tag)" + (r" gpu=(true|false)" if platform == "windows" else "")
    match = re.fullmatch(rf"{platform.capitalize()} build ([0-9a-f]{{40}}){suffix}", title)
    if match:
        return match.group(1)
    if run.get("event") == "workflow_dispatch":
        return None
    match = re.fullmatch(rf"{platform.capitalize()} build ([0-9a-f]{{40}})", title)
    return match.group(1) if match else None


def source_current(api, run, platform, default_branch, latest_id=None):
    event = run.get("event")
    if event not in ("pull_request", "workflow_dispatch") or run.get("path", "").split("@")[0] != ".github/workflows/" + SOURCE[platform]:
        return False
    if run.get("repository", {}).get("full_name", "").lower() != api.repo.lower() or run.get("head_repository", {}).get("full_name", "").lower() != api.repo.lower():
        return False
    head = run.get("head_sha", "")
    if not SHA.fullmatch(head):
        return None
    if latest_id is not None and run["id"] != latest_id:
        return False
    if event == "workflow_dispatch":
        merged = source_sha(run, platform)
        if not re.fullmatch(rf"{platform.capitalize()} build {merged} branch" +
                            (r" gpu=false" if platform == "windows" else ""),
                            run.get("display_title", "")):
            return None
        return merged if head == merged and run.get("head_branch") else None
    owner = api.repo.split("/")[0]
    branch = run.get("head_branch", "")
    if not branch:
        return False
    pulls = api.get("/pulls?" + urlencode({"state": "open", "head": owner + ":" + branch, "per_page": 100}))
    matches = [pr for pr in pulls if pr.get("head", {}).get("sha") == head and
               pr.get("head", {}).get("repo", {}).get("full_name", "").lower() == api.repo.lower() and
               pr.get("base", {}).get("ref") == default_branch]
    if len(matches) != 1:
        return False
    current_pr = api.get(f"/pulls/{matches[0]['number']}")
    merged = current_pr.get("merge_commit_sha", "")
    if not SHA.fullmatch(merged) or current_pr.get("state") != "open":
        return None
    titled_sha = source_sha(run, platform)
    if titled_sha and titled_sha != merged:
        return None
    # The fallback must test the immutable merge commit that the original PR
    # workflow checked out, with this PR head as its second parent.
    commit = api.get("/git/commits/" + merged)
    if len(commit.get("parents", [])) != 2 or commit["parents"][1]["sha"] != head:
        return False
    return merged


def fallback_eligible(api, run, platform, default_branch, tested_sha=None):
    """Only the default-branch workflow contract can be dispatched on hosted."""
    tested_sha = tested_sha or source_current(api, run, platform, default_branch)
    if not tested_sha or source_sha(run, platform) != tested_sha:
        return False
    workflow_path = ".github/workflows/" + SOURCE[platform]
    tested_file = api.get("/contents/" + workflow_path + "?ref=" + tested_sha)
    trusted_file = api.get("/contents/" + workflow_path + "?ref=" + default_branch)
    return tested_file.get("sha") == trusted_file.get("sha")


def latest_source_id(api, run, platform):
    query = urlencode({"event": run["event"], "head_sha": run["head_sha"], "per_page": 100})
    data = api.get("/actions/workflows/" + SOURCE[platform] + "/runs?" + query)
    if data["total_count"] > 100:
        raise ValueError("Too many matching source runs; refusing incomplete freshness check")
    matches = [item["id"] for item in data["workflow_runs"]
               if (item.get("head_branch") == run.get("head_branch") and
                   item.get("path", "").split("@")[0] == ".github/workflows/" + SOURCE[platform] and
                   item.get("repository", {}).get("full_name", "").lower() == api.repo.lower())]
    return max(matches) if matches else None


def jobs(api, run_id, attempt):
    data = api.get(f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    if data["total_count"] > 100:
        raise ValueError(f"Too many jobs in {run_id}/{attempt}")
    return data["jobs"]


def build_job(api, run_id, attempt):
    matches = [job for job in jobs(api, run_id, attempt)
               if job["name"] == "build" or job["name"].endswith(" / build")]
    if len(matches) != 1:
        raise ValueError(f"Expected one build job in {run_id}/{attempt}, found {len(matches)}")
    return matches[0]


def list_runs(api, workflow, event=None, status=None):
    query = {"per_page": 100}
    if event:
        query["event"] = event
    if status:
        query["status"] = status
    data = api.get("/actions/workflows/" + workflow + "/runs?" + urlencode(query))
    if data["total_count"] > 100:
        raise ValueError(f"More than 100 active runs for {workflow}; refusing incomplete scan")
    return data["workflow_runs"]


def host_runs(api, default_branch, allow_missing=False, active_only=False):
    found = {}
    try:
        runs = ([run for status in ("queued", "in_progress", "waiting", "pending", "requested")
                 for run in list_runs(api, "hosted-build-fallback.yml", "workflow_dispatch", status)]
                if active_only else list_runs(api, "hosted-build-fallback.yml", "workflow_dispatch"))
    except ApiError as error:
        if not (allow_missing and error.status == 404):
            raise
        print("DRY RUN: hosted fallback workflow is not on the default branch yet")
        runs = []
    for run in runs:
        match = HOST_TITLE.fullmatch(run.get("display_title", ""))
        if (match and run.get("event") == "workflow_dispatch" and
                run.get("head_branch") == default_branch and
                run.get("repository", {}).get("full_name", "").lower() == api.repo.lower() and
                run.get("path", "").split("@")[0] == ".github/workflows/hosted-build-fallback.yml"):
            platform, original, attempt, sha = match.groups()
            key = (platform, int(original), int(attempt), sha)
            found.setdefault(key, []).append(run)
    return found


def publish(api, sha, context, state, description, url):
    statuses = api.get("/commits/" + sha + "/statuses?per_page=100")
    previous = next((s for s in statuses if s["context"] == context), None)
    if previous and (previous["state"], previous["description"], previous["target_url"]) == (state, description, url):
        return
    api.post("/statuses/" + sha, {"state": state, "context": context,
                                    "description": description[:140], "target_url": url})
    print(f"Published {context}: {state} on {sha}: {description}")


def outcome(run):
    if run["status"] != "completed":
        return "pending"
    if run["conclusion"] == "success":
        return "success"
    return "failure" if run["conclusion"] in ("failure", "cancelled", "timed_out") else "error"


def dispatch_markers(api, platform, run_id, attempt):
    kinds = ["dispatch"] + [f"dispatch-{index}" for index in range(2, MAX_DISPATCH_ATTEMPTS + 1)]
    markers = [read_marker(api, kind, platform, run_id, attempt) for kind in kinds]
    if any(marker is None for marker in markers[:max((i for i, item in enumerate(markers) if item), default=-1) + 1]):
        raise ValueError("Dispatch reservation sequence has a gap")
    return [marker for marker in markers if marker]


def fresh_source(api, run_id, attempt, sha, platform, default_branch):
    run = api.get(f"/actions/runs/{run_id}")
    return (run["run_attempt"] == attempt and
            latest_source_id(api, run, platform) == run_id and
            source_current(api, run, platform, default_branch, run_id) == sha)


def control_one(api, run, platform, hosts, default_branch, delay, now, dry_run=False):
    run_id, attempt = run["id"], run["run_attempt"]
    sha = source_current(api, run, platform, default_branch)
    if not sha:
        print(f"Ignoring obsolete/untrusted {platform} source {run_id}/{attempt}")
        return
    context = MANUAL_CONTEXT[platform] if run["event"] == "workflow_dispatch" else CONTEXT[platform]
    key = (platform, run_id, attempt, sha)
    eligible = fallback_eligible(api, run, platform, default_branch, tested_sha=sha)
    claim = read_marker(api, "claims", platform, run_id, attempt)
    attempts = dispatch_markers(api, platform, run_id, attempt)
    if (claim and claim.get("sha") != sha) or any(item.get("sha") != sha for item in attempts):
        raise ValueError("Claim/dispatch SHA differs from original tested SHA")
    original_job = build_job(api, run_id, attempt)
    if not eligible:
        # The trusted source gate allows ordinary compilation, but the controller
        # never dispatches PR-edited workflow code or grants it a build claim.
        if source_sha(run, platform) != sha:
            print(f"Ordinary build {run_id}/{attempt} has an unverified run title; no stable status")
            return
        state = outcome(run) if original_job["status"] == "completed" else "pending"
        if state == "success" and (original_job["status"] != "completed" or original_job.get("conclusion") != "success"):
            state = "error"
        description = f"Ordinary build (fallback ineligible): {original_job['status']} {run.get('conclusion') or ''}"
        if fresh_source(api, run_id, attempt, sha, platform, default_branch):
            if dry_run:
                print(f"DRY RUN: would publish ordinary {state} for {run_id}/{attempt}")
            else:
                publish(api, run["head_sha"], context, state, description, run["html_url"])
        return
    candidates = []
    if original_job["status"] == "in_progress":
        candidates.append((original_job.get("started_at") or "", "self-hosted", run_id, attempt))
    # A user-dispatched lookalike is not eligible until this controller has
    # atomically recorded the delay decision.
    for host in hosts.get(key, []) if attempts else []:
        if datetime.fromisoformat(host["created_at"].replace("Z", "+00:00")) < datetime.fromisoformat(attempts[0]["created_at"]):
            continue
        if host["status"] == "in_progress":
            host_job = build_job(api, host["id"], host["run_attempt"])
            if host_job["status"] == "in_progress":
                candidates.append((host_job.get("started_at") or "", "hosted", host["id"], host["run_attempt"]))
    if claim is None and candidates:
        # Both jobs can start before this controller is scheduled. Atomic ref
        # creation picks one; earliest reported start is a deterministic preference.
        _, runner, winner_id, winner_attempt = min(candidates)
        if not fresh_source(api, run_id, attempt, sha, platform, default_branch):
            print(f"Skipping stale claim for {platform} {run_id}/{attempt}")
            return
        if dry_run:
            print(f"DRY RUN: would claim {platform} {run_id}/{attempt} for {runner} {winner_id}/{winner_attempt}")
        else:
            claim, _ = create_marker(api, "claims", platform, run_id, attempt, sha,
                                     runner=runner, winner_run=winner_id, winner_attempt=winner_attempt)
    queue_since = original_job.get("created_at")
    if original_job["status"] == "queued" and not queue_since:
        # Some job API variants omit created_at. The attempt endpoint supplies
        # a fresh run_started_at on rerun; never fall back to run.created_at.
        queue_since = api.get(f"/actions/runs/{run_id}/attempts/{attempt}").get("run_started_at")
    observed = hosts.get(key, [])
    can_retry = (not attempts or age_minutes(attempts[-1]["created_at"], now) >= DISPATCH_RETRY_MINUTES)
    if (claim is None and not observed and original_job["status"] == "queued" and
            run["status"] != "completed" and queue_since and age_minutes(queue_since, now) >= delay and
            len(attempts) < MAX_DISPATCH_ATTEMPTS and can_retry):
        if not fresh_source(api, run_id, attempt, sha, platform, default_branch):
            print(f"Skipping stale dispatch for {platform} {run_id}/{attempt}")
            return
        index = len(attempts) + 1
        kind = "dispatch" if index == 1 else f"dispatch-{index}"
        if dry_run:
            print(f"DRY RUN: would dispatch hosted {platform} attempt {index} for {run_id}/{attempt}")
        else:
            marker, new = create_marker(api, kind, platform, run_id, attempt, sha)
            if new:
                try:
                    api.post("/actions/workflows/hosted-build-fallback.yml/dispatches",
                             {"ref": default_branch, "inputs": {"platform": platform,
                              "original_run": str(run_id), "original_attempt": str(attempt), "sha": sha}}, expected=(204,))
                    print(f"Requested hosted {platform} fallback {index}/{MAX_DISPATCH_ATTEMPTS} for {run_id}/{attempt}")
                except (ApiError, OSError, RuntimeError) as error:
                    # 5xx/timeouts may be accepted despite a lost response. Reconcile
                    # observed runs before reserving the next bounded attempt.
                    print(f"Uncertain dispatch {index}/{MAX_DISPATCH_ATTEMPTS}: {error}")
            attempts.append(marker)
    if claim:
        winner = api.get(f"/actions/runs/{claim['winner_run']}")
        if claim.get("runner") == "self-hosted":
            if claim["winner_run"] != run_id or claim["winner_attempt"] != attempt:
                raise ValueError("Self-hosted claim points outside original run/attempt")
        elif claim.get("runner") == "hosted":
            if (winner.get("event") != "workflow_dispatch" or winner.get("head_branch") != default_branch or
                    winner.get("path", "").split("@")[0] != ".github/workflows/hosted-build-fallback.yml" or
                    winner.get("display_title") != f"Hosted fallback {platform} {run_id} {attempt} {sha}"):
                raise ValueError("Hosted winner identity mismatch")
        else:
            raise ValueError("Unknown claim runner")
        if winner["run_attempt"] != claim["winner_attempt"]:
            # A rerun is a new attempt and cannot inherit the old claim.
            state, description = "error", "Winning run was retried; inspect and rerun original attempt"
        else:
            state = outcome(winner)
            description = f"{claim['runner']} winner {claim['winner_run']}/{claim['winner_attempt']}: {winner['status']} {winner.get('conclusion') or ''}"
        url = winner["html_url"]
    elif run["status"] == "completed":
        state = outcome(run)
        if state == "success":
            state = "error"  # success without an acquired claim is invalid
        description = f"Original run ended without a build claim: {run.get('conclusion')}"
        url = run["html_url"]
    else:
        state, description, url = "pending", "Waiting for one claimed build", run["html_url"]
        if original_job["status"] == "queued" and not queue_since:
            state, description = "error", "Current attempt has no queue timestamp"
        elif observed and any(host["status"] == "completed" for host in observed):
            state, description = "error", "Hosted run ended before acquiring a build claim"
        elif observed and all(host["status"] in ("queued", "waiting", "pending", "requested") for host in observed) and age_minutes(min(host["created_at"] for host in observed), now) >= HOST_ALLOCATION_MINUTES:
            state, description = "error", "Hosted run allocated no build job within 30 minutes"
        elif len(attempts) == MAX_DISPATCH_ATTEMPTS and not observed and age_minutes(attempts[-1]["created_at"], now) >= DISPATCH_RETRY_MINUTES:
            state, description = "error", "Dispatch retries exhausted; no hosted run observed"
    # Re-read just before publication so a completed old attempt or superseded
    # PR run does not normally overwrite the current status. The status API has
    # no compare-and-set, so a final read/POST race remains documented.
    if not fresh_source(api, run_id, attempt, sha, platform, default_branch):
        print(f"Skipping stale status publication for {platform} {run_id}/{attempt}")
        return
    if dry_run:
        print(f"DRY RUN: would publish {context} {state} on {run['head_sha']}: {description}")
    else:
        publish(api, run["head_sha"], context, state, description, url)


def control(api, delay, now=None, dry_run=False, event=None):
    now = now or datetime.now(timezone.utc)
    repo = api.get("")
    default_branch = repo["default_branch"]
    trigger = (event or {}).get("workflow_run")
    hosts = host_runs(api, default_branch, allow_missing=dry_run, active_only=True)
    targets = []
    if trigger:
        path = trigger.get("path", "").split("@")[0]
        if path == ".github/workflows/hosted-build-fallback.yml":
            match = HOST_TITLE.fullmatch(trigger.get("display_title", ""))
            if match:
                targets.append((match.group(1), api.get(f"/actions/runs/{int(match.group(2))}")))
        else:
            for platform, workflow in SOURCE.items():
                if path == ".github/workflows/" + workflow:
                    targets.append((platform, api.get(f"/actions/runs/{trigger['id']}")))
    else:
        # Scheduled reconciliation asks only for active jobs. In particular a
        # rerun of an old run stays visible regardless of its original date.
        for platform, workflow in SOURCE.items():
            if os.environ.get(platform.upper() + "_RUNNER_CONFIGURED") != "true":
                continue
            for status in ("queued", "in_progress", "waiting", "pending", "requested"):
                for run in list_runs(api, workflow, status=status):
                    targets.append((platform, run))
        for (platform, original, _, _), runs in hosts.items():
            if runs:
                targets.append((platform, api.get(f"/actions/runs/{original}")))
    if trigger and trigger.get("path", "").split("@")[0] == ".github/workflows/hosted-build-fallback.yml":
        match = HOST_TITLE.fullmatch(trigger.get("display_title", ""))
        if match:
            key = (match.group(1), int(match.group(2)), int(match.group(3)), match.group(4))
            hosts.setdefault(key, []).append(api.get(f"/actions/runs/{trigger['id']}"))
    errors = []
    seen = set()
    for platform, run in targets:
        if os.environ.get(platform.upper() + "_RUNNER_CONFIGURED") != "true":
            continue
        key = (platform, run["id"], run["run_attempt"])
        if key in seen:
            continue
        seen.add(key)
        try:
            control_one(api, run, platform, hosts, default_branch, delay, now, dry_run)
        except (ApiError, ValueError, RuntimeError) as error:
            errors.append(f"{platform} {run['id']}/{run['run_attempt']}: {error}")
    if errors:
        raise RuntimeError("Controller errors (fail closed):\n" + "\n".join(errors))


def gate(api, platform, original_run, original_attempt, candidate_run, candidate_attempt, sha, output, wait_seconds=CLAIM_WAIT_SECONDS):
    source = api.get(f"/actions/runs/{original_run}")
    default_branch = api.get("")["default_branch"]
    latest = latest_source_id(api, source, platform)
    revision = source_current(api, source, platform, default_branch, latest)
    if source["run_attempt"] != original_attempt or revision != sha:
        print("Obsolete or untrusted source; skipping build")
        output.write_text("build=false\n", encoding="utf-8")
        return False
    if not fallback_eligible(api, source, platform, default_branch, tested_sha=sha):
        if candidate_run != original_run:
            raise ValueError("Hosted candidate has an ineligible source workflow")
        if not fresh_source(api, original_run, original_attempt, sha, platform, default_branch):
            raise RuntimeError("Ordinary source became obsolete before compilation")
        print("Fallback ineligible; running ordinary build and tests")
        output.write_text("build=true\n", encoding="utf-8")
        return True
    if candidate_run != original_run:
        hosted = api.get(f"/actions/runs/{candidate_run}")
        expected_title = f"Hosted fallback {platform} {original_run} {original_attempt} {sha}"
        if (hosted.get("event") != "workflow_dispatch" or hosted.get("head_branch") != default_branch or
                hosted.get("path", "").split("@")[0] != ".github/workflows/hosted-build-fallback.yml" or
                hosted.get("display_title") != expected_title or hosted.get("run_attempt") != candidate_attempt):
            raise ValueError("Hosted candidate identity mismatch")
    deadline = time.monotonic() + wait_seconds
    while True:
        claim = read_marker(api, "claims", platform, original_run, original_attempt)
        if claim:
            winner = claim.get("winner_run") == candidate_run and claim.get("winner_attempt") == candidate_attempt and claim.get("sha") == sha
            if winner and not fresh_source(api, original_run, original_attempt, sha, platform, default_branch):
                raise RuntimeError("Winning source became obsolete before compilation; failing closed")
            print(f"Build claim winner: {claim['winner_run']}/{claim['winner_attempt']}; this job {'builds' if winner else 'exits without compiling'}")
            output.write_text(f"build={'true' if winner else 'false'}\n", encoding="utf-8")
            return winner
        if time.monotonic() >= deadline:
            raise RuntimeError(f"No trusted build claim after {wait_seconds}s; failing closed (controller/API/runner allocation)")
        time.sleep(min(20, max(0, deadline - time.monotonic())))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    control_parser = sub.add_parser("control")
    control_parser.add_argument("--delay-minutes", type=int, default=10)
    control_parser.add_argument("--dry-run", action="store_true")
    gate_parser = sub.add_parser("gate")
    for name in ("platform", "original-run", "original-attempt", "candidate-run", "candidate-attempt", "sha", "output"):
        gate_parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if not os.environ.get("GH_TOKEN"):
        parser.error("GH_TOKEN is required")
    api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    try:
        if args.command == "control":
            if not 1 <= args.delay_minutes <= 120:
                raise ValueError("Fallback delay must be 1..120 minutes")
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8")) if os.environ.get("GITHUB_EVENT_PATH") else None
            control(api, args.delay_minutes, dry_run=args.dry_run, event=event)
        else:
            if args.platform not in SOURCE or not SHA.fullmatch(args.sha):
                raise ValueError("Invalid platform or SHA")
            gate(api, args.platform, int(args.original_run), int(args.original_attempt),
                 int(args.candidate_run), int(args.candidate_attempt), args.sha, Path(args.output))
    except (ApiError, KeyError, OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"CI build controller: {error}\n")


if __name__ == "__main__":
    main()
