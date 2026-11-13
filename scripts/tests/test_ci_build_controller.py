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
        self.job = {"name": "build", "status": "queued", "created_at": stamp(queued), "started_at": None, "conclusion": None}
        self.hosts = []
        self.tags = {}
        self.refs = {}
        self.statuses = []
        self.dispatches = []
        self.pull_open = True
        self.current_merge = MERGE
        self.workflow_trusted = True
        self.fail_path = None
        self.dispatch_failure_after_accept = False
        self.dispatch_timeout_after_accept = False
        self.attempt_started_at = stamp(queued)

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
        if path.startswith("/contents/.github/workflows/"):
            return {"sha": "d" * 40 if self.workflow_trusted or path.endswith("ref=master") else "e" * 40}
        if path.startswith("/git/ref/tags/"):
            return self.refs.get(path.removeprefix("/git/ref/tags/"))
        if path.startswith("/git/tags/"):
            return self.tags[path.removeprefix("/git/tags/")]
        if path == "/actions/runs/101":
            return self.source
        if path.startswith("/actions/runs/101/attempts/") and "/jobs?" not in path:
            return {"run_started_at": self.attempt_started_at}
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
            if self.dispatch_failure_after_accept:
                raise ci.ApiError(503, path, "response lost after acceptance")
            if self.dispatch_timeout_after_accept:
                raise TimeoutError("response timed out after acceptance")
            return None
        if path.startswith("/statuses/"):
            self.statuses.append(dict(data))
            return data
        raise AssertionError(f"unexpected POST {path}")

    def add_host(self, status="in_progress", minutes_ago=1, result=None, host_id=202):
        host = {"id": host_id, "run_attempt": 1, "event": "workflow_dispatch",
                "path": ".github/workflows/hosted-build-fallback.yml",
                "head_branch": "master", "repository": {"full_name": self.repo},
                "display_title": f"Hosted fallback {self.platform} 101 1 {MERGE}",
                "status": status, "conclusion": result,
                "created_at": stamp(minutes_ago),
                "html_url": f"https://github.com/Zethrus/Murmur/actions/runs/{host_id}",
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

    def age_dispatch(self, kind="dispatch", minutes=4):
        name = ci.ref_name(kind, "linux", 101, self.api.source["run_attempt"])
        tag = self.api.tags[self.api.refs[name]["object"]["sha"]]
        message = json.loads(tag["message"])
        message["created_at"] = stamp(minutes)
        tag["message"] = json.dumps(message)

    def test_queued_threshold_and_duplicate_controller_dispatch(self):
        self.api.job["created_at"] = stamp(9)
        self.control()
        self.assertEqual(self.api.dispatches, [])
        self.api.job["created_at"] = stamp(10)
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
        self.api.job["created_at"] = stamp(12)
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

    def test_changed_pr_workflow_cannot_claim_or_dispatch(self):
        self.api.workflow_trusted = False
        self.control()
        self.assertEqual(self.api.dispatches, [])
        self.assertEqual(self.api.refs, {})
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            self.assertTrue(ci.gate(self.api, "linux", 101, 1, 101, 1, MERGE, output, 0))
            self.assertEqual(output.read_text(), "build=true\n")
        self.api.job.update(status="completed", conclusion="success")
        self.api.source.update(status="completed", conclusion="success")
        self.control()
        self.assertEqual(self.api.statuses[-1]["state"], "success")

    def test_changed_workflow_skipped_job_cannot_publish_green(self):
        self.api.workflow_trusted = False
        self.api.job.update(status="completed", conclusion="skipped")
        self.api.source.update(status="completed", conclusion="success")
        self.control()
        self.assertEqual(self.api.statuses[-1]["state"], "error")

    def test_changed_workflow_run_title_still_compiles_ordinarily(self):
        self.api.workflow_trusted = False
        self.api.source["display_title"] = "Workflow experiment"
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            self.assertTrue(ci.gate(self.api, "linux", 101, 1, 101, 1, MERGE, output, 0))
            self.assertEqual(output.read_text(), "build=true\n")
        self.control()
        self.assertEqual(self.api.dispatches, [])
        self.assertEqual(self.api.statuses, [])

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

    def test_same_second_dispatch_is_eligible_but_earlier_run_is_not(self):
        for seconds_before in (0, 1):
            with self.subTest(seconds_before=seconds_before):
                self.api = FakeGitHub()
                self.control()
                reservation = ci.read_marker(self.api, "dispatch", "linux", 101, 1)
                recorded = datetime.fromisoformat(reservation["created_at"])
                host = self.api.add_host()
                host["created_at"] = (recorded.replace(microsecond=0) -
                                      timedelta(seconds=seconds_before)).isoformat()
                self.control()
                claim = ci.read_marker(self.api, "claims", "linux", 101, 1)
                if seconds_before:
                    self.assertIsNone(claim)
                else:
                    self.assertEqual(claim["winner_run"], 202)

    def test_hosted_win_loser_gate_and_aggregate_failure(self):
        self.api.job["created_at"] = stamp(12)
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
        self.control()
        self.assertEqual(self.api.dispatches, [])
        self.assertIsNotNone(ci.read_marker(self.api, "dispatch", "linux", 101, 1))
        self.assertEqual(self.api.statuses[-1]["state"], "pending")

    def test_lost_dispatch_response_reconciles_observed_run_without_retry(self):
        self.api.dispatch_failure_after_accept = True
        self.control()
        self.assertEqual(len(self.api.dispatches), 1)
        self.api.add_host(status="queued", minutes_ago=-1)
        self.age_dispatch(minutes=4)
        self.control()
        self.assertEqual(len(self.api.dispatches), 1)
        self.assertIsNone(ci.read_marker(self.api, "dispatch-2", "linux", 101, 1))

    def test_dispatch_timeout_after_acceptance_is_reconciled(self):
        self.api.dispatch_timeout_after_accept = True
        self.control()
        self.assertEqual(len(self.api.dispatches), 1)
        self.api.add_host(status="queued", minutes_ago=-1)
        self.age_dispatch(minutes=4)
        self.control()
        self.assertEqual(len(self.api.dispatches), 1)

    def test_duplicate_accepted_dispatches_claim_only_one_compiler(self):
        self.control()
        self.age_dispatch(minutes=4)
        self.control()  # no observed run yet: a bounded second dispatch
        self.api.add_host(status="in_progress", minutes_ago=-1, host_id=202)
        self.api.add_host(status="in_progress", minutes_ago=-1, host_id=203)
        self.control()
        claim = ci.read_marker(self.api, "claims", "linux", 101, 1)
        self.assertIn(claim["winner_run"], (202, 203))
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            results = [ci.gate(self.api, "linux", 101, 1, host_id, 1, MERGE, output, 0)
                       for host_id in (202, 203)]
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)

    def test_unobserved_dispatch_retries_then_publishes_terminal_error(self):
        self.api.fail_path = "/actions/workflows/hosted-build-fallback.yml/dispatches"
        self.control()
        self.age_dispatch()
        self.control()
        self.assertIsNotNone(ci.read_marker(self.api, "dispatch-2", "linux", 101, 1))
        self.age_dispatch("dispatch-2")
        self.control()
        self.assertIsNotNone(ci.read_marker(self.api, "dispatch-3", "linux", 101, 1))
        self.age_dispatch("dispatch-3")
        self.control()
        self.assertEqual(self.api.statuses[-1]["state"], "error")
        self.assertIn("Dispatch retries exhausted", self.api.statuses[-1]["description"])

    def test_hosted_preclaim_failure_is_terminal(self):
        self.control()
        self.api.add_host(status="completed", minutes_ago=-1, result="failure")
        self.control()
        self.assertEqual(self.api.statuses[-1]["state"], "error")
        self.assertIn("before acquiring", self.api.statuses[-1]["description"])

    def test_hosted_allocation_stall_is_terminal(self):
        self.control()
        self.age_dispatch(minutes=40)
        self.api.add_host(status="queued", minutes_ago=31)
        self.control()
        self.assertEqual(self.api.statuses[-1]["state"], "error")
        self.assertIn("allocated no build job", self.api.statuses[-1]["description"])

    def test_rerun_old_run_uses_new_attempt_job_queue_age(self):
        self.api.source["created_at"] = stamp(10 * 24 * 60)
        self.api.source["run_attempt"] = 2
        self.api.job["created_at"] = stamp(2)
        self.control()
        self.assertEqual(self.api.dispatches, [])
        self.api.job["created_at"] = stamp(11)
        self.control()
        self.assertEqual(len(self.api.dispatches), 1)
        self.assertEqual(self.api.dispatches[0]["inputs"]["original_attempt"], "2")

    def test_attempt_start_is_used_when_job_creation_time_missing(self):
        self.api.source["created_at"] = stamp(10 * 24 * 60)
        self.api.source["run_attempt"] = 2
        self.api.job.pop("created_at")
        self.api.attempt_started_at = stamp(2)
        self.control()
        self.assertEqual(self.api.dispatches, [])
        self.api.attempt_started_at = stamp(11)
        self.control()
        self.assertEqual(len(self.api.dispatches), 1)

    def test_no_attempt_timestamp_is_visible_error(self):
        self.api.job.pop("created_at")
        self.api.attempt_started_at = None
        self.control()
        self.assertEqual(self.api.dispatches, [])
        self.assertEqual(self.api.statuses[-1]["state"], "error")

    def test_stale_attempt_before_claim_or_status_does_not_publish(self):
        self.api.job.update(status="in_progress", started_at=stamp(1))
        with mock.patch.object(ci, "fresh_source", return_value=False):
            self.control()
        self.assertIsNone(ci.read_marker(self.api, "claims", "linux", 101, 1))
        self.assertEqual(self.api.statuses, [])

    def test_status_freshness_recheck_skips_superseded_attempt(self):
        self.api.job["created_at"] = stamp(2)
        with mock.patch.object(ci, "fresh_source", return_value=False):
            self.control()
        self.assertEqual(self.api.statuses, [])

    def test_gate_rechecks_attempt_after_reading_claim(self):
        self.api.job.update(status="in_progress", started_at=stamp(1))
        self.control()
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(ci, "fresh_source", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "obsolete before compilation"):
                ci.gate(self.api, "linux", 101, 1, 101, 1, MERGE, Path(temp) / "out", 0)

    def test_manual_branch_build_fallback_and_tag_gpu_exclusions(self):
        self.api.source.update(event="workflow_dispatch", head_sha=MERGE,
                               display_title=f"Linux build {MERGE} branch")
        self.control()
        self.assertEqual(len(self.api.dispatches), 1)
        self.assertEqual(self.api.statuses[-1]["context"], ci.MANUAL_CONTEXT["linux"])
        self.api = FakeGitHub()
        self.api.source.update(event="workflow_dispatch", head_sha=MERGE,
                               display_title=f"Linux build {MERGE} tag")
        self.control()
        self.assertEqual(self.api.dispatches, [])
        windows = FakeGitHub("windows")
        windows.source.update(event="workflow_dispatch", head_sha=MERGE,
                              display_title=f"Windows build {MERGE} branch gpu=true")
        ci.control_one(windows, windows.source, "windows", {}, "master", 10, NOW)
        self.assertEqual(windows.dispatches, [])

    def test_claim_retained_after_long_queue_does_not_duplicate(self):
        self.api.job.update(status="in_progress", started_at=stamp(1))
        self.control()
        self.api.job.update(status="queued", created_at=stamp(24 * 60))
        self.control()
        self.assertEqual(self.api.dispatches, [])
        self.assertIsNotNone(ci.read_marker(self.api, "claims", "linux", 101, 1))

    def test_scheduled_scan_includes_old_active_rerun(self):
        self.api.source.update(run_attempt=2, created_at=stamp(10 * 24 * 60))
        self.api.job["created_at"] = stamp(11)
        with mock.patch.dict(ci.os.environ, {"LINUX_RUNNER_CONFIGURED": "true", "WINDOWS_RUNNER_CONFIGURED": "false"}):
            ci.control(self.api, 10, NOW)
        self.assertEqual(len(self.api.dispatches), 1)
        self.assertEqual(self.api.dispatches[0]["inputs"]["original_attempt"], "2")


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


class WorkflowContractTests(unittest.TestCase):
    def test_linux_release_requires_successful_windows_or_explicit_disable(self):
        release = (ROOT / '.github/workflows/release.yml').read_text()
        for job in ('linux-build', 'linux'):
            block = release.split('  ' + job + ':\n', 1)[1].split('    permissions:', 1)[0].split('    uses:', 1)[0]
            condition = block.split('    if: >-\n', 1)[1].strip()
            expression = condition.replace('always()', 'True').replace('&&', ' and ').replace('||', ' or ')
            for enabled in ('true', 'false'):
                for built in ('success', 'failure', 'cancelled', 'skipped'):
                    for uploaded in ('success', 'failure', 'cancelled', 'skipped'):
                        evaluated = expression
                        for name, value in {
                            "needs.prepare.result": 'success',
                            "needs['linux-build'].result": 'success',
                            "needs['windows-build'].result": built,
                            'needs.windows.result': uploaded,
                            'vars.ENABLE_WINDOWS': enabled,
                            'vars.MURMUR_RELEASE_TRUST_FINGERPRINT': 'configured',
                        }.items():
                            evaluated = evaluated.replace(name, repr(value))
                        expected = ((built == uploaded == 'success') or
                                    (enabled != 'true' and built == uploaded == 'skipped'))
                        self.assertEqual(eval(' '.join(evaluated.split()), {'__builtins__': {} }), expected,
                                         (job, enabled, built, uploaded))

    def test_reusable_builds_are_read_only_and_tag_writes_are_separate(self):
        workflows = ROOT / ".github" / "workflows"
        release = (workflows / "release.yml").read_text()
        hosted = (workflows / "hosted-build-fallback.yml").read_text()
        self.assertIn("permissions:\n  contents: read\n  actions: read", hosted)
        self.assertIn("permissions:\n  contents: write\n  actions: read", release)
        for platform in ("linux", "windows"):
            build = (workflows / f"{platform}-build.yml").read_text()
            self.assertIn("permissions:\n  contents: read\n  actions: read", build)
            self.assertNotIn("contents: write", build)
            self.assertNotIn("cancel-in-progress:", build)
        self.assertIn("gh release upload", release)
        self.assertNotIn("download-artifact", release)
        self.assertNotIn("murmur-windows-release-assets", release)
        self.assertIn("Stage Windows assets on the trusted release runner", (workflows / "windows-build.yml").read_text())
        self.assertIn("value: ${{ jobs.build.outputs.stage_attempt }}", (workflows / "windows-build.yml").read_text())
        self.assertIn("BUILD_ATTEMPT: ${{ needs['windows-build'].outputs.stage_attempt }}", release)
        self.assertIn("--attempt $env:BUILD_ATTEMPT", release)
        flipper = (workflows / "flipper-build.yml").read_text()
        self.assertIn("permissions:\n  contents: read", flipper)
        self.assertNotIn("gh release upload", flipper)

    def test_manual_non_gpu_branch_contract_is_explicit(self):
        workflows = ROOT / ".github" / "workflows"
        for platform in ("linux", "windows"):
            build = (workflows / f"{platform}-build.yml").read_text()
            self.assertIn("github.ref_type == 'branch'", build)
            self.assertIn("github.event_name == 'workflow_dispatch'", build)
            self.assertIn("github.ref_type", build.splitlines()[1])
        windows = (workflows / "windows-build.yml").read_text()
        self.assertIn("!inputs.gpu", windows)
        self.assertIn("gpu={1}", windows.splitlines()[1])

if __name__ == "__main__":
    unittest.main()
