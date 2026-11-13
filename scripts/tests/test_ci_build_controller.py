"""Controller decisions and immutable claim protocol without live Actions runs."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ci_build_controller", ROOT / "scripts" / "ci-build-controller.py")
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)
HEAD = "b" * 40
MERGE = "a" * 40
NOW = datetime.now(timezone.utc)


def stamp(minutes_ago=0):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


class FakeGitHub:
    repo = "Zethrus/Murmur"

    def __init__(self, platform="linux", queued=12):
        self.platform = platform
        self.source = {"id": 101, "run_attempt": 1, "event": "pull_request",
                       "path": f".github/workflows/{platform}-build.yml", "head_sha": HEAD,
                       "display_title": f"{platform.capitalize()} build {MERGE}",
                       "head_branch": "feature", "head_repository": {"full_name": self.repo},
                       "repository": {"full_name": self.repo}, "status": "queued",
                       "conclusion": None, "created_at": stamp(queued),
                       "html_url": "https://github.com/Zethrus/Murmur/actions/runs/101"}
        self.job = {"name": "build", "status": "queued", "started_at": None}
        self.hosts = []
        self.tags = {}
        self.refs = {}
        self.statuses = []
        self.dispatches = []
        self.pull_open = True
        self.current_merge = MERGE
        self.fail_path = None

    def get(self, path, missing=False):
        if path == self.fail_path:
            raise ci.ApiError(503, path, "unavailable")
        if path == "":
            return {"default_branch": "master"}
        if path.startswith("/pulls?"):
            return ([{"number": 7, "head": {"sha": HEAD, "repo": {"full_name": self.repo}},
                      "base": {"ref": "master"}}] if self.pull_open else [])
        if path == "/pulls/7":
            return {"state": "open", "merge_commit_sha": self.current_merge}
        if path == "/git/commits/" + MERGE:
            return {"parents": [{"sha": "c" * 40}, {"sha": HEAD}]}
        if path.startswith("/git/ref/tags/"):
            return self.refs.get(path.removeprefix("/git/ref/tags/"))
        if path.startswith("/git/tags/"):
            return self.tags[path.removeprefix("/git/tags/")]
        if path == "/actions/runs/101":
            return self.source
        if path.startswith("/actions/runs/101/attempts/"):
            return {"total_count": 1, "jobs": [self.job]}
        if path.startswith("/actions/runs/") and "/attempts/" in path:
            host_id = int(path.split("/")[3])
            return {"total_count": 1, "jobs": [next(h["job"] for h in self.hosts if h["id"] == host_id)]}
        if path.startswith("/actions/runs/"):
            host_id = int(path.split("/")[3])
            return next(h for h in self.hosts if h["id"] == host_id)
        if path.startswith("/actions/workflows/hosted-build-fallback.yml/runs?"):
            return {"total_count": len(self.hosts), "workflow_runs": self.hosts}
        if path.startswith("/actions/workflows/") and "/runs?" in path:
            return {"total_count": 1, "workflow_runs": [self.source]}
        if path.startswith("/commits/") and "/statuses?" in path:
            return list(reversed(self.statuses))
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, data, expected=(201,)):
        if path == self.fail_path:
            raise ci.ApiError(503, path, "unavailable")
        if path == "/git/tags":
            sha = hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()
            self.tags[sha] = dict(data)
            return {"sha": sha}
        if path == "/git/refs":
            name = data["ref"].removeprefix("refs/tags/")
            if name in self.refs:
                raise ci.ApiError(422, path, "already exists")
            self.refs[name] = {"object": {"type": "tag", "sha": data["sha"]}}
            return self.refs[name]
        if path == "/actions/workflows/hosted-build-fallback.yml/dispatches":
            self.dispatches.append(data)
            return None
        if path.startswith("/statuses/"):
            self.statuses.append(dict(data))
            return data
        raise AssertionError(f"unexpected POST {path}")

    def add_host(self, status="in_progress", minutes_ago=1, result=None):
        host = {"id": 202, "run_attempt": 1, "event": "workflow_dispatch",
                "path": ".github/workflows/hosted-build-fallback.yml",
                "head_branch": "master", "repository": {"full_name": self.repo},
                "display_title": f"Hosted fallback {self.platform} 101 1 {MERGE}",
                "status": status, "conclusion": result,
                "created_at": stamp(minutes_ago),
                "html_url": "https://github.com/Zethrus/Murmur/actions/runs/202",
                "job": {"name": "linux / build", "status": status,
                        "started_at": stamp(minutes_ago) if status == "in_progress" else None}}
        self.hosts.append(host)
        return host


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()

    def control(self, dry_run=False):
        return ci.control_one(self.api, self.api.source, "linux", ci.host_runs(self.api, "master"),
                              "master", 10, NOW, dry_run)

    def test_queued_threshold_and_duplicate_controller_dispatch(self):
        self.api.source["created_at"] = stamp(9)
        self.control()
        self.assertEqual(self.api.dispatches, [])
        self.api.source["created_at"] = stamp(10)
        self.control()
        self.control()
        self.assertEqual(len(self.api.dispatches), 1)
        self.assertEqual(self.api.dispatches[0]["inputs"]["sha"], MERGE)
        self.assertEqual(self.api.statuses[-1]["context"], ci.CONTEXT["linux"])
        self.assertEqual(self.api.statuses[-1]["state"], "pending")
        self.assertEqual(len(self.api.statuses), 1)

    def test_cancelled_original_does_not_fallback(self):
        self.api.source.update(status="completed", conclusion="cancelled")
        self.api.job.update(status="completed")
        self.control()
        self.assertEqual(self.api.dispatches, [])
        self.assertEqual(self.api.statuses[-1]["state"], "failure")

    def test_retry_has_new_attempt_identity(self):
        self.control()
        self.api.source["run_attempt"] = 2
        self.control()
        self.assertEqual(len(self.api.dispatches), 2)
        self.assertIsNotNone(ci.read_marker(self.api, "dispatch", "linux", 101, 1))
        self.assertIsNotNone(ci.read_marker(self.api, "dispatch", "linux", 101, 2))

    def test_changed_test_merge_and_foreign_host_are_ignored(self):
        self.api.current_merge = "c" * 40
        self.control()
        self.assertEqual(self.api.dispatches, [])
        self.api.current_merge = MERGE
        self.control()
        host = self.api.add_host(minutes_ago=-1)
        host["head_branch"] = "untrusted-feature"
        self.control()
        self.assertIsNone(ci.read_marker(self.api, "claims", "linux", 101, 1))

    def test_running_self_hosted_claim_prevents_fallback_and_wins_race(self):
        self.api.job.update(status="in_progress", started_at=stamp(2))
        self.control()
        claim = ci.read_marker(self.api, "claims", "linux", 101, 1)
        self.assertEqual(claim["winner_run"], 101)
        self.assertEqual(self.api.dispatches, [])
        self.api.add_host(minutes_ago=1)
        self.control()
        self.assertEqual(ci.read_marker(self.api, "claims", "linux", 101, 1)["winner_run"], 101)
        self.api.source.update(status="completed", conclusion="failure")
        self.api.job["status"] = "completed"
        self.control()
        self.assertEqual(self.api.statuses[-1]["state"], "failure")
        self.assertEqual(self.api.dispatches, [])

    def test_self_hosted_success_is_stable_result(self):
        self.api.job.update(status="in_progress", started_at=stamp(1))
        self.api.source["status"] = "in_progress"
        self.control()
        self.api.job["status"] = "completed"
        self.api.source.update(status="completed", conclusion="success")
        self.control()
        self.assertEqual(self.api.statuses[-1]["state"], "success")
        self.assertEqual(self.api.statuses[-1]["context"], ci.CONTEXT["linux"])

    def test_hosted_win_loser_gate_and_aggregate_failure(self):
        self.api.source["created_at"] = stamp(12)
        self.control()
        host = self.api.add_host(minutes_ago=-1)
        self.control()
        claim = ci.read_marker(self.api, "claims", "linux", 101, 1)
        self.assertEqual(claim["winner_run"], 202)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            self.assertFalse(ci.gate(self.api, "linux", 101, 1, 101, 1, MERGE, output, 0))
            self.assertEqual(output.read_text(), "build=false\n")
            self.assertTrue(ci.gate(self.api, "linux", 101, 1, 202, 1, MERGE, output, 0))
        host.update(status="completed", conclusion="failure")
        self.api.source.update(status="completed", conclusion="success")  # loser result is irrelevant
        self.control()
        self.assertEqual(self.api.statuses[-1]["state"], "failure")
        self.assertEqual(self.api.statuses[-1]["target_url"], host["html_url"])
        self.assertEqual(len(self.api.dispatches), 1)

    def test_simultaneous_running_candidates_choose_one_claim(self):
        self.control()  # dispatch guard must exist before hosted candidate is eligible
        host = self.api.add_host(minutes_ago=-1)
        host["job"]["started_at"] = stamp(-2)
        self.api.job.update(status="in_progress", started_at=stamp(-3))
        self.control()
        self.assertEqual(ci.read_marker(self.api, "claims", "linux", 101, 1)["winner_run"], 202)
        self.assertEqual(len([name for name in self.api.refs if name.startswith("ci-build-claims/")]), 1)

    def test_obsolete_attempt_fork_and_closed_pr_never_dispatch(self):
        self.api.source["run_attempt"] = 2
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            self.assertFalse(ci.gate(self.api, "linux", 101, 1, 101, 1, MERGE, output, 0))
        self.api.source["run_attempt"] = 1
        self.api.source["head_repository"]["full_name"] = "outside/Murmur"
        self.control()
        self.assertEqual(self.api.dispatches, [])
        self.api.source["head_repository"]["full_name"] = self.api.repo
        self.api.pull_open = False
        self.control()
        self.assertEqual(self.api.dispatches, [])

    def test_api_error_fails_closed_and_is_visible(self):
        self.api.fail_path = "/actions/workflows/hosted-build-fallback.yml/dispatches"
        with self.assertRaisesRegex(ci.ApiError, "503"):
            self.control()
        self.assertEqual(self.api.dispatches, [])
        self.assertIsNotNone(ci.read_marker(self.api, "dispatch", "linux", 101, 1))

    def test_atomic_ref_collision_keeps_first_winner(self):
        first, created = ci.create_marker(self.api, "claims", "linux", 101, 1, MERGE,
                                          runner="self-hosted", winner_run=101, winner_attempt=1)
        second, created_again = ci.create_marker(self.api, "claims", "linux", 101, 1, MERGE,
                                                  runner="hosted", winner_run=202, winner_attempt=1)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first, second)
        self.assertEqual(second["winner_run"], 101)

    def test_unexplained_422_is_not_treated_as_collision(self):
        original_post = self.api.post
        def failing_post(path, data, expected=(201,)):
            if path == "/git/refs":
                raise ci.ApiError(422, path, "validation failed")
            return original_post(path, data, expected)
        with mock.patch.object(self.api, "post", side_effect=failing_post):
            with self.assertRaisesRegex(ci.ApiError, "422"):
                ci.create_marker(self.api, "claims", "linux", 101, 1, MERGE,
                                 runner="self-hosted", winner_run=101, winner_attempt=1)

    def test_dry_run_has_no_writes(self):
        self.control(dry_run=True)
        self.assertEqual(self.api.refs, {})
        self.assertEqual(self.api.statuses, [])
        self.assertEqual(self.api.dispatches, [])

    def test_claim_timeout_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, "No trusted build claim"):
                ci.gate(self.api, "linux", 101, 1, 101, 1, MERGE, Path(temp) / "out", 0)


if __name__ == "__main__":
    unittest.main()
