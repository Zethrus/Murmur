"""Pure parts of the Windows desktop harness (scripts/windows/desktop), runnable on any OS."""
import hashlib
import json
import multiprocessing
from pathlib import Path
import re
import shutil
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest import mock
import wave

HARNESS = Path(__file__).resolve().parents[1] / "windows" / "desktop"
sys.path.insert(0, str(HARNESS))

import catalog  # noqa: E402
from checks import setup as setup_checks  # noqa: E402
import fixtures  # noqa: E402
import golden  # noqa: E402
import guard  # noqa: E402
import guard_diagnostic  # noqa: E402
import handoff  # noqa: E402
import manual  # noqa: E402
import murmur  # noqa: E402
import report  # noqa: E402
import restore  # noqa: E402
import run  # noqa: E402
import tomlconfig  # noqa: E402
import win32  # noqa: E402
from context import Fail, Skip  # noqa: E402

SHA = "a" * 40
OTHER = "b" * 40


def fake_observer_worker(connection):
    """Spawn-safe test peer for the parent guard's process handshake."""
    connection.send(("ready", {"probes_seen": 1}))
    if connection.recv() == "stop":
        connection.send(("diagnostics", {"probes_seen": 2}))
    connection.close()


def fake_early_input_worker(connection):
    connection.send(("user_input", "real keyboard input during startup"))
    connection.close()


def fake_diagnostic_worker(connection, diagnostic):
    connection.send(("ready", {"probes_seen": 1, "diagnostic": diagnostic}))
    if connection.recv() == "stop":
        connection.send(("diagnostics", {"probes_seen": 2, "diagnostic": diagnostic}))
    connection.close()


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ImportTests(unittest.TestCase):
    def test_windows_modules_import_on_any_os(self):
        import capture  # noqa: F401
        import context  # noqa: F401
        import murmur  # noqa: F401
        import targets  # noqa: F401
        import uia  # noqa: F401
        self.assertEqual(win32.parse_combo("Ctrl+Alt+Shift+F9"), (["ctrl", "alt", "shift"], "f9"))
        with self.assertRaises(ValueError):
            win32.parse_combo("Ctrl+Alt")
        with self.assertRaises(ValueError):
            win32.parse_combo("Ctrl+Hyper+X")

    def test_guard_injected_flags(self):
        self.assertTrue(guard.is_injected_key(0x10))
        self.assertFalse(guard.is_injected_key(0x00))
        self.assertTrue(guard.is_injected_mouse(0x01))
        self.assertFalse(guard.is_injected_mouse(0x00))


class ReportTests(unittest.TestCase):
    def result(self, status, reason="", attempt=1, **extra):
        return report.Result("H1", status, reason, "Hold", "hotkeys", attempt=attempt, **extra)

    def test_fail_and_skip_need_a_reason(self):
        with self.assertRaises(ValueError):
            report.Result("H1", report.FAIL)
        with self.assertRaises(ValueError):
            report.Result("H1", report.SKIPPED)
        with self.assertRaises(ValueError):
            report.Result("H1", "maybe", "x")

    def test_aggregate_marks_flaky_failures(self):
        row = report.aggregate([self.result(report.PASS), self.result(report.FAIL, "late", 2),
                                self.result(report.PASS, attempt=3)])
        self.assertEqual(row.status, report.FAIL)
        self.assertIn("flaky: failed 1 of 3", row.reason)
        self.assertEqual(row.attempt, 3)

    def test_aggregate_all_skipped_stays_skipped(self):
        row = report.aggregate([self.result(report.SKIPPED, "icon hidden")])
        self.assertEqual((row.status, row.reason), (report.SKIPPED, "icon hidden"))

    def test_markdown_has_one_row_per_id_and_escapes_pipes(self):
        attempts = [self.result(report.PASS, artifacts=["C:/out/H1-notepad.png"]),
                    report.Result("I3", report.SKIPPED, "no | buffer", best_effort=True)]
        text = report.markdown({"sha": SHA}, attempts)
        self.assertIn("| H1 | pass |  | `H1-notepad.png` |", text)
        self.assertIn("| I3 (best effort) | skipped | no \\| buffer |  |", text)
        self.assertIn("1 pass · 0 fail · 1 skipped · 0 aborted", text)

    def test_write_and_load_round_trip_and_manual_merge(self):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work)
        attempts = [self.result(report.PASS), report.Result("S2", report.SKIPPED, "manual")]
        report.write(work, {"sha": SHA}, attempts)
        meta, loaded = report.load(work / "desktop-report.json")
        self.assertEqual(meta["sha"], SHA)
        self.assertEqual([r.id for r in loaded], ["H1", "S2"])
        merged = report.merge(loaded, [report.Result("S2", report.PASS, source="manual")])
        self.assertEqual([(r.id, r.status) for r in merged], [("H1", report.PASS), ("S2", report.PASS)])
        data = json.loads((work / "desktop-report.json").read_text())
        self.assertEqual(data["schema"], report.SCHEMA)
        self.assertEqual(len(data["rows"]), 2)


class GoldenTests(unittest.TestCase):
    def test_compare(self):
        self.assertIsNone(golden.compare("The fox. ", "The fox. "))
        self.assertIn("differs at character 4", golden.compare("The fox. ", "The Fox. "))
        self.assertIn("more than once", golden.compare("Hi there. ", "Hi there. Hi there. "))
        self.assertIn("nothing delivered", golden.compare("x", None))
        self.assertIn("lengths 4 vs 3", golden.compare("abcd", "abc"))

    def test_store_records_first_delivery_and_checks_later_ones(self):
        store = golden.GoldenStore()
        key = golden.key("A", "hold", "Ctrl+Alt+Shift+F9")
        self.assertEqual(key, golden.key("A", "hold", "ctrl+alt+shift+f9"))
        with self.assertRaises(ValueError):
            store.record(key, "  ")
        store.record(key, "The quick brown fox. ")
        with self.assertRaises(ValueError):
            store.record(key, "again")
        self.assertIsNone(store.check(key, "The quick brown fox. "))
        self.assertIsNotNone(store.check(key, "The quick brown fox."))
        with self.assertRaises(KeyError):
            store.check(golden.key("B"), "x")
        self.assertNotEqual(key, golden.key("A", "hold", "Ctrl+Alt+Shift+F9", style="code"))


class ConfigWriterTests(unittest.TestCase):
    def test_partial_config_round_trips_through_toml(self):
        config = tomlconfig.merge(run.base_config("Ctrl+Alt+Shift+F9"), {
            "postproc": {"replacements": {"marker phrase": "café déjà vu", 'say "x"': "y\\z"}},
            "injection": {"exclude_apps": ["notepad"], "focus_change": "refocus"},
            "overlay": {"scale": 1.6, "placement": {"x_fraction": 0.25, "monitor_width": 1920}},
        })
        text = tomlconfig.dumps(config)
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["postproc"]["replacements"]["marker phrase"], "café déjà vu")
        self.assertEqual(parsed["postproc"]["replacements"]['say "x"'], "y\\z")
        self.assertEqual(parsed["injection"]["exclude_apps"], ["notepad"])
        self.assertEqual(parsed["overlay"]["placement"]["x_fraction"], 0.25)
        self.assertEqual(parsed["general"]["hotkey"], "Ctrl+Alt+Shift+F9")
        self.assertLess(text.index("[overlay]"), text.index("[overlay.placement]"))

    def test_keys_and_values_follow_the_app_schema(self):
        for bad in ({"general": {"hot_key": "x"}}, {"nope": {"a": 1}}, {"general": {"hotkey_mode": "press"}},
                    {"injection": {"focus_change": "steal"}}, {"overlay": {"scale": 2.0}},
                    {"general": {"autostart": "yes"}}, {"injection": {"exclude_apps": [1]}},
                    {"audio": {"silence_timeout_ms": 50}}, {"engine": {"beam_size": True}}):
            with self.subTest(bad=bad), self.assertRaises(tomlconfig.ConfigError):
                tomlconfig.dumps(bad)

    def test_schema_keys_exist_in_config_rs(self):
        source = (HARNESS.parents[2] / "src-tauri" / "src" / "config.rs").read_text(encoding="utf-8")
        for section, keys in tomlconfig.SCHEMA.items():
            for key in keys:
                with self.subTest(key=f"{section}.{key}"):
                    self.assertRegex(source, rf"\b{key}: ")

    def test_merge_replaces_replacements_and_deep_merges_tables(self):
        base = {"general": {"hotkey": "A", "theme": "light"}, "postproc": {"replacements": {"a": "b"}}}
        merged = tomlconfig.merge(base, {"general": {"theme": "dark"}, "postproc": {"replacements": {"c": "d"}}})
        self.assertEqual(merged, {"general": {"hotkey": "A", "theme": "dark"}, "postproc": {"replacements": {"c": "d"}}})
        self.assertEqual(base["general"]["theme"], "light")


class CatalogTests(unittest.TestCase):
    def test_47_automated_checks_with_unique_ids(self):
        ids = [c.id for c in catalog.CHECKS]
        self.assertEqual(len(ids), 47)
        self.assertEqual(len(set(ids)), 47)
        self.assertEqual({c.id for c in catalog.CHECKS if c.best_effort}, {"T1", "T2", "I3", "K1"})

    def test_every_check_function_exists_in_its_group_module(self):
        for check in catalog.CHECKS:
            source = (HARNESS / "checks" / f"{check.group}.py").read_text(encoding="utf-8")
            with self.subTest(check=check.id):
                self.assertRegex(source, rf"(?m)^def {check.function}\(ctx\):")
                self.assertTrue(check.function.startswith(check.id.lower() + "_"))

    def test_automated_and_manual_ids_cover_the_checklist(self):
        text = (HARNESS.parents[2] / "docs" / "parity" / "windows-checklist.md").read_text(encoding="utf-8")
        listed = set(re.findall(r"(?m)^\| ([A-Z]\d+) ", text))
        covered = {c.id for c in catalog.CHECKS} | {i.split("-")[0] for i, _ in catalog.MANUAL}
        self.assertEqual(listed - covered, set())

    def test_selection(self):
        self.assertEqual([c.id for c in catalog.select("f2, h1")], ["H1", "F2"])
        self.assertEqual([c.group for c in catalog.select(group="focus")], ["focus"] * 11)
        self.assertEqual([c.id for c in catalog.select("H1,F2", "focus")], ["F2"])
        self.assertEqual(catalog.select()[0].id, "P1")
        self.assertEqual(catalog.select()[-1].id, "C3")
        with self.assertRaisesRegex(catalog.SelectionError, "manual"):
            catalog.select("S2")
        with self.assertRaisesRegex(catalog.SelectionError, "Unknown group"):
            catalog.select(group="network")
        with self.assertRaisesRegex(catalog.SelectionError, "matches no checks"):
            catalog.select("H1", "focus")
        self.assertEqual(catalog.parse_ids(" h1 ;H1, f13 "), ["H1", "F13"])

    def test_overlay_error_state_check_is_ordered_before_the_drag(self):
        overlay = [c.id for c in catalog.select(group="overlay")]
        self.assertEqual(overlay[0], "O1")


class RestoreTests(unittest.TestCase):
    def test_undo_runs_newest_first_and_survives_failures(self):
        calls = []
        restorer = restore.Restorer(log=lambda _: None)
        restorer.push("first", lambda: calls.append(1))
        restorer.push("broken", lambda: 1 / 0)
        restorer.push("third", lambda: calls.append(3))
        errors = restorer.restore_all()
        self.assertEqual(calls, [3, 1])
        self.assertEqual([label for label, _ in errors], ["broken"])
        self.assertEqual(restorer.pending, [])

    def test_restore_to_mark_keeps_session_level_entries(self):
        calls = []
        restorer = restore.Restorer()
        restorer.push("session", lambda: calls.append("session"))
        mark = restorer.mark()
        token = restorer.push("check", lambda: calls.append("check"))
        restorer.push("check 2", lambda: calls.append("check 2"))
        restorer.discard(token)
        restorer.restore_to(mark)
        self.assertEqual(calls, ["check 2"])
        self.assertEqual(restorer.pending, ["session"])

    def test_registry_value_restores_missing_and_existing_values(self):
        class Backend:
            def __init__(self):
                self.data = {("Run", "Murmur"): ("C:\\owner\\murmur.exe --background", 1)}

            def read(self, path, name):
                return self.data.get((path, name), restore.MISSING)

            def write(self, path, name, value, kind):
                self.data[(path, name)] = (value, kind)

            def delete(self, path, name):
                del self.data[(path, name)]

        backend, restorer = Backend(), restore.Restorer()
        restore.RegistryValue(backend, "Run", "Murmur").set(restorer, "C:\\test\\murmur.exe", 1)
        restore.RegistryValue(backend, "Consent", "Value").set(restorer, "Deny", 1)
        self.assertEqual(backend.data[("Consent", "Value")], ("Deny", 1))
        restorer.restore_all()
        self.assertEqual(backend.data, {("Run", "Murmur"): ("C:\\owner\\murmur.exe --background", 1)})


class HandoffTests(unittest.TestCase):
    FILES = {
        "Murmur_0.5.0_x64-setup.exe": b"installer",
        "murmur-windows-x86_64.zip": b"portable",
        "murmur-engine-windows-x86_64-0.5.0.zip": b"engine",
        "murmur-cuda-windows-x86_64-0.5.0.zip": b"cuda",
        "components-manifest.json": b"{}",
        "components-manifest.json.sig": b"sig",
        "murmur-release.asc": b"cert",
        "components-windows-inputs.json": b"[]",
    }

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.source = self.work / "murmur-ci-out" / SHA
        self.source.mkdir(parents=True)
        for name, data in self.FILES.items():
            (self.source / name).write_bytes(data)
        self.sums = {name: digest(data) for name, data in self.FILES.items()}
        (self.source / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in sorted(self.sums.items())))

    def log(self, commit=SHA, sums=None):
        lines = ["build\tHand the build\t2026-11-09T10:00:00.0000000Z ##[group]Run $Commit = $env:GITHUB_SHA",
                 'build\tHand the build\t2026-11-09T10:00:00.1Z   Write-Host "MURMUR-HANDOFF-SHA256 $Commit $Hash $($File.Name)"']
        for name, value in sorted((sums or self.sums).items()):
            lines.append(f"build\tHand the build\t2026-11-09T10:00:01.0000000Z MURMUR-HANDOFF-SHA256 {commit} {value} {name}\r")
        return "\n".join(lines)

    def gh(self, runs, logs):
        def fake(*arguments):
            if arguments[0] == "api":
                self.assertIn(f"head_sha={SHA}", arguments[1])
                return json.dumps({"workflow_runs": runs})
            self.assertEqual(arguments[:2], ("run", "view"))
            return logs[int(arguments[2])]
        return fake

    def test_sha_must_be_the_dispatched_commit(self):
        self.assertEqual(handoff.check_sha(SHA.upper(), SHA), SHA)
        with self.assertRaisesRegex(handoff.HandoffError, "not the dispatched commit"):
            handoff.check_sha(OTHER, SHA)
        with self.assertRaisesRegex(handoff.HandoffError, "40-character"):
            handoff.check_sha("abc123", "abc123")
        with self.assertRaises(handoff.HandoffError):
            handoff.check_sha("", "")

    def test_sums_parsing_is_strict(self):
        self.assertEqual(handoff.parse_sums((self.source / "SHA256SUMS").read_text()), self.sums)
        for bad in (f"{'0' * 64} ../evil.exe\n", f"{'0' * 64}  sub/file\n", "zz  file\n", "",
                    f"{'0' * 64}  a\n{'1' * 64}  a\n"):
            with self.subTest(bad=bad), self.assertRaises(handoff.HandoffError):
                handoff.parse_sums(bad)

    def test_log_parsing_ignores_the_script_echo_and_other_commits(self):
        text = self.log() + "\n" + self.log(commit=OTHER, sums={"murmur-windows-x86_64.zip": "f" * 64})
        self.assertEqual(handoff.parse_log(text, SHA), self.sums)
        conflicting = self.log() + f"\nMURMUR-HANDOFF-SHA256 {SHA} {'f' * 64} murmur-windows-x86_64.zip"
        with self.assertRaisesRegex(handoff.HandoffError, "two different hashes"):
            handoff.parse_log(conflicting, SHA)

    def test_compare_and_required_files(self):
        handoff.compare(self.sums, dict(self.sums))
        with self.assertRaisesRegex(handoff.HandoffError, "no MURMUR-HANDOFF"):
            handoff.compare(self.sums, {})
        with self.assertRaisesRegex(handoff.HandoffError, "disagrees"):
            handoff.compare(self.sums, dict(self.sums, **{"murmur-release.asc": "0" * 64}))
        handoff.check_required(self.sums)
        with self.assertRaisesRegex(handoff.HandoffError, "Unexpected"):
            handoff.check_required(dict(self.sums, **{"evil.dll": "0" * 64}))
        missing = dict(self.sums)
        del missing["murmur-release.asc"]
        with self.assertRaisesRegex(handoff.HandoffError, "murmur-release.asc"):
            handoff.check_required(missing)

    def test_verify_copies_and_checks_against_the_matching_log(self):
        runs = [{"id": 2, "head_sha": SHA, "conclusion": "success", "run_started_at": "2026-11-09T10:00:00Z",
                 "html_url": "https://example.invalid/2", "event": "pull_request"},
                {"id": 1, "head_sha": SHA, "conclusion": "success", "run_started_at": "2026-11-08T10:00:00Z",
                 "html_url": "https://example.invalid/1", "event": "workflow_dispatch"}]
        stale = dict(self.sums, **{"murmur-windows-x86_64.zip": "0" * 64})
        dest = self.work / "copy"
        record = handoff.verify(SHA, SHA, self.source, dest, "o/r", self.gh(runs, {2: self.log(sums=stale), 1: self.log()}))
        self.assertEqual(record["build_run"]["id"], 1)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), sorted([*self.FILES, "build.json"]))
        self.assertEqual(json.loads((dest / "build.json").read_text())["sha"], SHA)

    def test_tampered_folder_is_refused(self):
        runs = [{"id": 1, "head_sha": SHA, "conclusion": "success", "event": "workflow_dispatch"}]
        fake = self.gh(runs, {1: self.log()})
        (self.source / "murmur-windows-x86_64.zip").write_bytes(b"tampered")
        with self.assertRaisesRegex(handoff.HandoffError, "murmur-windows-x86_64.zip: SHA-256"):
            handoff.verify(SHA, SHA, self.source, self.work / "copy", "o/r", fake)
        # Rewriting SHA256SUMS to match does not help: the build log disagrees.
        sums = dict(self.sums, **{"murmur-windows-x86_64.zip": digest(b"tampered")})
        (self.source / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in sorted(sums.items())))
        with self.assertRaisesRegex(handoff.HandoffError, "No build log matches"):
            handoff.verify(SHA, SHA, self.source, self.work / "copy", "o/r", fake)

    def test_extra_file_and_wrong_commit_are_refused(self):
        runs = [{"id": 1, "head_sha": SHA, "conclusion": "success"}]
        (self.source / "extra.dll").write_bytes(b"x")
        with self.assertRaisesRegex(handoff.HandoffError, "differs from SHA256SUMS"):
            handoff.verify(SHA, SHA, self.source, self.work / "copy", "o/r", self.gh(runs, {1: self.log()}))
        with self.assertRaisesRegex(handoff.HandoffError, "not the dispatched commit"):
            handoff.verify(SHA, OTHER, self.source, self.work / "copy", "o/r", self.gh(runs, {1: self.log()}))

    def test_no_successful_build_run(self):
        with self.assertRaisesRegex(handoff.HandoffError, "No successful"):
            handoff.matching_run("o/r", SHA, self.sums, self.gh([], {}))


class FixtureTests(unittest.TestCase):
    def test_padding_appends_silence_and_reports_speech_length(self):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work)
        source = work / "speech.wav"
        with wave.open(str(source), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(fixtures.RATE)
            writer.writeframes(b"\x01\x00" * fixtures.RATE * 2)
        speech = fixtures.pad_silence(source, work / "padded.wav", 20.0)
        self.assertAlmostEqual(speech, 2.0)
        self.assertAlmostEqual(fixtures.duration(work / "padded.wav"), 22.0)
        with wave.open(str(work / "padded.wav"), "rb") as reader:
            reader.setpos(fixtures.RATE * 2)
            self.assertEqual(set(reader.readframes(100)), {0})

    def test_padding_outlasts_the_longest_hold_and_stays_below_the_utterance_limit(self):
        self.assertGreater(fixtures.PAD_SECONDS, 15)
        self.assertLess(fixtures.PAD_SECONDS + 10, 90)

    def test_sentences_cover_the_checks(self):
        self.assertGreater(len(fixtures.SENTENCES["LONG"]), 60)
        self.assertIn("d", fixtures.SENTENCES["D"].lower())
        self.assertIn(fixtures.REPLACEMENT[0], fixtures.SENTENCES["MARKER"])
        self.assertNotEqual(fixtures.cache_name("A", "x"), fixtures.cache_name("A", "y"))

    def test_speak_script_escapes_quotes(self):
        script = fixtures.speak_script("It's here", Path("C:/t/o'k.wav"))
        self.assertIn("Speak('It''s here')", script)
        self.assertIn("o''k.wav", script)


class FakeContext:
    def __init__(self, work):
        self.restorer = restore.Restorer(log=lambda _: None)
        self.guard = guard.NoGuard()
        self.out = Path(work)
        self.artifacts, self.notes, self.check_id = [], [], ""

    def begin(self, check_id):
        self.check_id, self.artifacts, self.notes = check_id, [], []

    def artifact(self, suffix):
        path = self.out / f"{self.check_id}-{suffix}"
        self.artifacts.append(str(path))
        return path

    def note(self, text):
        self.notes.append(text)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.ctx = FakeContext(self.work)

    def run_with(self, behaviours, ids, repeat=1):
        undone = []

        def lookup(check):
            def function(ctx):
                ctx.restorer.push(f"undo {check.id}", lambda: undone.append(check.id))
                behaviours[check.id](ctx)
            return function

        with mock.patch.object(win32, "release_all") as release_all:
            results, aborted = run.run_checks(self.ctx, catalog.select(ids), repeat, lookup, log=lambda _: None)
        release_all.assert_called()
        return results, aborted, undone

    def test_statuses_and_per_check_restore(self):
        def fail(_):
            raise Fail("text differs")

        def skip(_):
            raise Skip("VS Code not installed")

        def crash(_):
            raise RuntimeError("boom")

        results, aborted, undone = self.run_with(
            {"H1": lambda c: None, "H2": fail, "I2": skip, "T1": crash, "I1": crash}, "H1,H2,I1,I2,T1")
        statuses = {r.id: (r.status, r.reason) for r in results}
        self.assertEqual(statuses["H1"], (report.PASS, ""))
        self.assertEqual(statuses["H2"], (report.FAIL, "text differs"))
        self.assertEqual(statuses["I2"], (report.SKIPPED, "VS Code not installed"))
        self.assertEqual(statuses["I1"][0], report.FAIL)
        self.assertEqual(statuses["T1"][0], report.SKIPPED)  # best effort: never a pass, never a crash
        self.assertIn("could not run", statuses["T1"][1])
        self.assertEqual(aborted, "")
        self.assertEqual(undone, ["H1", "H2", "I1", "I2", "T1"])
        self.assertTrue((self.work / "T1-harness-error.txt").is_file())

    def test_user_input_aborts_the_remaining_checks(self):
        def touched(_):
            raise guard.UserInput("real keyboard input")

        results, aborted, undone = self.run_with({"H1": lambda c: None, "H2": touched}, "H1,H2,H3", repeat=2)
        self.assertIn("user input", aborted)
        self.assertEqual([(r.id, r.status) for r in results],
                         [("H1", report.PASS), ("H2", report.ABORTED), ("H3", report.ABORTED),
                          ("H1", report.ABORTED), ("H2", report.ABORTED), ("H3", report.ABORTED)])
        self.assertEqual(undone, ["H1", "H2"])

    def test_lost_input_guard_aborts_without_claiming_real_user_activity(self):
        def lost(_):
            raise guard.GuardUnavailable("probe not observed")

        results, aborted, undone = self.run_with({"H1": lost}, "H1,H2")
        self.assertIn("input guard unavailable", aborted)
        self.assertNotIn("user input", aborted)
        self.assertEqual([r.status for r in results], [report.ABORTED, report.ABORTED])
        self.assertEqual(undone, ["H1"])

    def test_guard_loss_during_a_check_cannot_be_reported_as_pass(self):
        self.ctx.guard = SimpleNamespace(check=mock.Mock(side_effect=[None, guard.GuardUnavailable("probe lost")]))
        results, aborted, _ = self.run_with({"H1": lambda _: None}, "H1,H2")
        self.assertIn("input guard unavailable", aborted)
        self.assertEqual([r.status for r in results], [report.ABORTED, report.ABORTED])

    def test_p4_guard_loss_records_exact_handoff_and_preserves_launch_evidence(self):
        stages = []
        self.ctx.guard = SimpleNamespace(check=lambda: None, mark=stages.append)
        self.ctx.fixtures = {"A": SimpleNamespace(path=self.work / "a.wav")}

        def lost_in_ensure(_wav):
            raise guard.GuardUnavailable("probe lost during main session launch")

        def save_startup(artifact):
            artifact("startup-log.txt").write_text("main session starting\n", encoding="utf-8")

        self.ctx.murmur = SimpleNamespace(ensure=lost_in_ensure, save_startup_diagnostics=save_startup)
        with mock.patch.object(win32, "release_all"):
            results, aborted = run.run_checks(self.ctx, catalog.select("P4,S1"), 1,
                                               lambda check: setup_checks.p4_components if check.id == "P4"
                                               else lambda _: None, log=lambda _: None)
        self.assertEqual([r.status for r in results], [report.ABORTED, report.ABORTED])
        self.assertIn("input guard unavailable", aborted)
        self.assertEqual(stages, ["P4:1:begin", "P4:ensure:begin", "P4:1:guard-abort"])
        self.assertEqual((self.work / "P4-startup-log.txt").read_text(), "main session starting\n")
        self.assertIn(str(self.work / "P4-startup-log.txt"), results[0].artifacts)

    def test_startup_failure_preserves_evidence_and_stops_cascading_checks(self):
        session = murmur.MurmurSession(SimpleNamespace(), self.work, SimpleNamespace(), {})
        session.process = SimpleNamespace(pid=42, poll=lambda: 101)
        session._launch_output = self.work / "murmur-launch-1.txt"
        session._launch_output.write_bytes(b"panic from app\r\n")
        session.log_path.parent.mkdir(parents=True)
        session.log_path.write_bytes(b"old log\r\n")
        session._log_offset = session.log_path.stat().st_size
        with session.log_path.open("ab") as stream:
            stream.write(b"new launch error\r\n")

        def startup(_):
            raise murmur.StartupError("Murmur exited before its Settings window appeared (exit code 101)", session)

        with mock.patch.object(murmur.win32, "enum_windows", return_value=[(12, 42, "Murmur Overlay", "WebView")]), \
             mock.patch.object(murmur.win32, "user32", SimpleNamespace(IsWindowVisible=lambda _: False)), \
             mock.patch.object(win32, "IS_WINDOWS", True):
            results, blocked, undone = self.run_with({"P3": startup}, "P3,P4,S1")
        self.assertEqual([r.status for r in results], [report.FAIL, report.ABORTED, report.ABORTED])
        self.assertIn("P3 startup failure", blocked)
        self.assertEqual(undone, ["P3"])
        self.assertEqual((self.work / "P3-startup-output.txt").read_text(), "panic from app\n")
        self.assertEqual((self.work / "P3-startup-log.txt").read_text(), "new launch error\n")
        self.assertIn("Murmur Overlay", (self.work / "P3-startup-windows.txt").read_text())
        self.assertIn(str(self.work / "P3-startup-log.txt"), results[0].artifacts)

    def test_startup_diagnostics_preserve_crlf_and_replace_invalid_utf8(self):
        session = murmur.MurmurSession(SimpleNamespace(), self.work, SimpleNamespace(), {})
        session._launch_output = self.work / "murmur-launch-1.txt"
        session._launch_output.write_bytes(b"panic from app\r\ninvalid: \xff\r\n")
        session.log_path.parent.mkdir(parents=True)
        session.log_path.write_bytes(b"old log\r\n")
        session._log_offset = session.log_path.stat().st_size
        with session.log_path.open("ab") as stream:
            stream.write(b"new launch error\r\ninvalid: \xff\r\n")

        def windows_write_text(path, data, encoding=None):
            # Simulate Windows text-mode newline translation on any host OS.
            return path.write_bytes(data.replace("\n", "\r\n").encode(encoding or "utf-8"))

        with mock.patch.object(Path, "write_text", windows_write_text), \
             mock.patch.object(murmur.win32, "enum_windows", return_value=[]):
            session.save_startup_diagnostics(lambda suffix: self.work / suffix)

        replacement = "\ufffd".encode("utf-8")
        self.assertEqual((self.work / "startup-output.txt").read_bytes(),
                         b"panic from app\r\ninvalid: " + replacement + b"\r\n")
        self.assertEqual((self.work / "startup-log.txt").read_bytes(),
                         b"new launch error\r\ninvalid: " + replacement + b"\r\n")

    def test_startup_window_wait_reports_early_process_exit(self):
        session = murmur.MurmurSession(SimpleNamespace(), self.work, SimpleNamespace(sleep=lambda _: None), {})
        session.process = SimpleNamespace(poll=lambda: 101)
        with mock.patch.object(murmur.win32, "wait_for", side_effect=lambda predicate, *_args, **_kwargs: predicate()):
            with self.assertRaisesRegex(murmur.StartupError, "exit code 101") as raised:
                session._wait_for_window(lambda: 0, "Settings window")
        self.assertIs(raised.exception.session, session)

    def test_launch_env_requests_backtrace_in_the_isolated_profile(self):
        session = murmur.MurmurSession(SimpleNamespace(certificate=self.work / "ci.asc"), self.work,
                                       SimpleNamespace(), {})
        env = session.env()
        self.assertEqual(env["RUST_BACKTRACE"], "1")
        self.assertEqual(env["APPDATA"], str(session.appdata))
        self.assertEqual(env["LOCALAPPDATA"], str(session.localappdata))

    def test_preflight_reports_every_problem(self):
        class FakeWin32:
            def input_desktop(self):
                return None

            def pids(self, name):
                return {"murmur.exe": [42], "Notepad.exe": [7]}.get(name, [])

            def idle_seconds(self):
                return 5.0

        problems = run.preflight(self.work, catalog.select("H1"), FakeWin32())
        text = "\n".join(problems)
        for expected in ("not a verified build", "locked", "another Murmur", "Notepad is running", "idle for 60"):
            self.assertIn(expected, text)
        (self.work / "build.json").write_text("{}")
        self.assertEqual(len(run.preflight(self.work, catalog.select("F1"), FakeWin32())), 3)

    def test_guard_diagnostic_preflight_needs_no_build_and_refuses_busy_desktop(self):
        fake = SimpleNamespace(input_desktop=lambda: "Default", pids=lambda _: [], idle_seconds=lambda: 61.0)
        self.assertEqual(run.preflight_desktop(fake), [])
        fake.idle_seconds = lambda: 5.0
        problems = run.preflight_desktop(fake)
        with mock.patch.object(guard_diagnostic.win32, "require_windows"), \
             mock.patch.object(guard_diagnostic.run, "preflight_desktop", return_value=problems):
            result = guard_diagnostic.diagnose(1)
        self.assertEqual(result["status"], "preflight_refused")
        self.assertIn("idle for 60", "\n".join(result["problems"]))


class ManualTests(unittest.TestCase):
    def test_answers_items_and_results(self):
        self.assertEqual(manual.parse_answer(" P "), report.PASS)
        self.assertEqual(manual.parse_answer("fail"), report.FAIL)
        self.assertIsNone(manual.parse_answer("maybe"))
        self.assertEqual([i for i, _ in manual.items("k2,s2")], ["S2", "K2"])
        with self.assertRaises(catalog.SelectionError):
            manual.items("H1")
        self.assertEqual(set(manual.STEPS), {i for i, _ in catalog.MANUAL})
        failed = manual.manual_result("K2", "Narrator", report.FAIL, "")
        self.assertEqual((failed.reason, failed.source), ("no note given", "manual"))
        passed = manual.manual_result("K3", "Motion", report.PASS, "reduced")
        self.assertEqual((passed.reason, passed.notes), ("", ["reduced"]))


if __name__ == "__main__":
    unittest.main()


class GuardMouseTests(unittest.TestCase):
    """The observer's pure decision behind the low-level mouse hook."""

    def test_injected_input_never_trips(self):
        g = guard._HookObserver()
        self.assertIsNone(g.mouse_event(guard.WM_MOUSEMOVE, True, (0, 0)))
        self.assertIsNone(g.mouse_event(0x0201, True, (0, 0)))  # WM_LBUTTONDOWN, injected

    def test_real_button_trips(self):
        self.assertEqual(guard._HookObserver().mouse_event(0x0201, False, (5, 5)), "real mouse button or wheel input")

    def test_real_drift_within_tolerance_is_ignored_and_beyond_trips(self):
        g = guard._HookObserver()
        self.assertIsNone(g.mouse_event(guard.WM_MOUSEMOVE, False, (100, 100)))
        self.assertIsNone(g.mouse_event(guard.WM_MOUSEMOVE, False, (102, 99)))
        self.assertEqual(g.mouse_event(guard.WM_MOUSEMOVE, False, (110, 100)), "real mouse movement")

    def test_injected_move_reanchors_the_origin(self):
        # Real jitter at the old place, the harness moves the cursor far away, then a pixel of real
        # jitter where the cursor now is: not a person.
        g = guard._HookObserver()
        self.assertIsNone(g.mouse_event(guard.WM_MOUSEMOVE, False, (100, 100)))
        self.assertIsNone(g.mouse_event(guard.WM_MOUSEMOVE, True, (900, 700)))
        self.assertIsNone(g.mouse_event(guard.WM_MOUSEMOVE, False, (901, 700)))
        self.assertIsNone(g.mouse_event(guard.WM_MOUSEMOVE, False, (902, 701)))
        self.assertEqual(g.mouse_event(guard.WM_MOUSEMOVE, False, (920, 701)), "real mouse movement")

    def test_probe_is_a_reserved_key_with_a_tag(self):
        self.assertEqual(guard.PROBE_VK, 0x88)  # reserved virtual-key code
        self.assertLess(guard.PROBE_TIMEOUT, guard.PROBE_INTERVAL)

    def test_desktop_context_reads_hook_thread_not_calling_thread(self):
        def session_id(_pid, out):
            win32.ctypes.cast(out, win32.ctypes.POINTER(win32.wintypes.DWORD)).contents.value = 9
            return True

        def object_info(handle, index, out, _size, _needed):
            if index == 6:
                win32.ctypes.cast(out, win32.ctypes.POINTER(win32.wintypes.BOOL)).contents.value = 1
            else:
                name = "WinSta0" if handle == 10 else "Default"
                buffer = win32.ctypes.create_unicode_buffer(name)
                win32.ctypes.memmove(out, buffer, win32.ctypes.sizeof(buffer))
            return True

        get_thread_desktop = mock.Mock(return_value=20)
        fake_user32 = SimpleNamespace(GetProcessWindowStation=lambda: 10,
                                      GetThreadDesktop=get_thread_desktop,
                                      GetUserObjectInformationW=object_info,
                                      GetForegroundWindow=lambda: 30)
        fake_kernel32 = SimpleNamespace(GetCurrentThreadId=lambda: 77, ProcessIdToSessionId=session_id)
        with mock.patch.object(win32, "require_windows"), \
             mock.patch.object(win32, "user32", fake_user32), \
             mock.patch.object(win32, "kernel32", fake_kernel32):
            context = win32.desktop_context(123)
        get_thread_desktop.assert_called_once_with(123)
        self.assertEqual(context, {"session_id": 9, "window_station": "WinSta0",
                                   "thread_desktop": "Default", "thread_desktop_receives_input": True,
                                   "foreground_window_present": True})

    def test_hook_context_diagnostic_survives_observer_stop(self):
        watcher = guard._HookObserver(diagnostic=True)
        watcher._desktop_context = {"window_station": "WinSta0",
                                    "thread_desktop_receives_input": True}
        with mock.patch.object(win32, "input_desktop", return_value="Default"):
            state = watcher.diagnostics()["hook_state"]["desktop_context"]
        self.assertEqual(state, watcher._desktop_context)

    def test_full_run_mark_snapshots_probe_state_without_clearing_guard_loss(self):
        watcher = guard.Guard(diagnostic=True)
        watcher._started_at = guard.time.monotonic()
        watcher._process = SimpleNamespace(is_alive=lambda: True)
        watcher._diagnostics = {"probes_seen": 21,
                                "hook_state": {"keyboard_callbacks": 21, "heartbeat_received": 21}}
        watcher.mark("P4:ensure:begin")
        watcher._trip("probe lost", unavailable=True)
        watcher.mark("P4:1:guard-abort")
        timeline = watcher.diagnostics()["timeline"]
        self.assertEqual([point["stage"] for point in timeline], ["P4:ensure:begin", "P4:1:guard-abort"])
        self.assertEqual([point["probes_seen"] for point in timeline], [21, 21])
        self.assertEqual([point["tripped"] for point in timeline], [False, True])
        with self.assertRaises(guard.GuardUnavailable):
            watcher.check()

    def test_diagnostic_records_only_injected_probe_candidates_and_exact_match(self):
        watcher = guard._HookObserver(diagnostic=True)
        watcher._expected_probe_tag = guard.PROBE_TAG | (2 << 32)
        real = SimpleNamespace(vkCode=0x41, scanCode=30, flags=0, dwExtraInfo=0)
        unrelated = SimpleNamespace(vkCode=0x41, scanCode=30, flags=guard.LLKHF_INJECTED,
                                    dwExtraInfo=0)
        wrong_tag = SimpleNamespace(vkCode=guard.PROBE_VK, scanCode=0, flags=guard.LLKHF_INJECTED,
                                    dwExtraInfo=guard.PROBE_TAG | (1 << 32))
        exact = SimpleNamespace(vkCode=guard.PROBE_VK, scanCode=0, flags=guard.LLKHF_INJECTED,
                                dwExtraInfo=watcher._expected_probe_tag)
        for event in (real, unrelated, wrong_tag, exact):
            watcher._record_injected_key(0x101, event)
        self.assertEqual(watcher._injected_key_events, 3)
        self.assertEqual([item["matches_expected"] for item in watcher._probe_events], [False, True])
        self.assertNotIn("0x41", str(watcher._probe_events))

    def test_diagnostic_mouse_probe_has_no_displacement_or_button_action(self):
        watcher = guard._HookObserver(diagnostic=True)
        tag = guard.PROBE_TAG | (1 << 32)
        send = mock.Mock(return_value=1)
        with mock.patch.object(win32, "user32", SimpleNamespace(SendInput=send)):
            watcher._send_mouse_probe(tag)
        count, events, size = send.call_args.args
        self.assertEqual((count, size), (1, guard.ctypes.sizeof(win32.INPUT)))
        self.assertEqual(events[0].type, win32.INPUT_MOUSE)
        self.assertEqual((events[0].mi.dx, events[0].mi.dy, events[0].mi.mouseData), (0, 0, 0))
        self.assertEqual(events[0].mi.dwFlags,
                         win32.MOUSEEVENTF_MOVE | win32.MOUSEEVENTF_MOVE_NOCOALESCE)
        self.assertEqual(events[0].mi.dwExtraInfo, tag)

    def test_diagnostic_probe_adds_evidence_but_still_fails_closed(self):
        watcher = guard._HookObserver(diagnostic=True)
        with mock.patch.object(guard, "PROBE_TIMEOUT", 0.001), \
             mock.patch.object(watcher, "_post_heartbeat") as heartbeat, \
             mock.patch.object(watcher, "_send_mouse_probe") as mouse_probe, \
             mock.patch.object(watcher, "_send_probe", return_value=True) as key_probe:
            watcher._watch()
        heartbeat.assert_called_once()
        mouse_probe.assert_called_once_with(key_probe.call_args.args[0])
        with self.assertRaises(guard.GuardUnavailable):
            watcher.check()
        self.assertEqual(watcher.probes_seen, 0)

    def test_diagnostic_does_not_send_key_after_real_input_during_mouse_probe(self):
        watcher = guard._HookObserver(diagnostic=True)

        def real_input(_tag):
            watcher._trip("real mouse movement")

        with mock.patch.object(watcher, "_post_heartbeat"), \
             mock.patch.object(watcher, "_send_mouse_probe", side_effect=real_input), \
             mock.patch.object(watcher, "_send_probe") as key_probe:
            watcher._watch()
        key_probe.assert_not_called()
        with self.assertRaises(guard.UserInput):
            watcher.check()

    def test_diagnostic_redacts_real_key_reason(self):
        with mock.patch.object(guard_diagnostic.win32, "require_windows"), \
             mock.patch.object(guard_diagnostic.win32, "desktop_context",
                               return_value={"window_station": "WinSta0"}), \
             mock.patch.object(guard_diagnostic.run, "preflight_desktop", return_value=[]), \
             mock.patch.object(guard_diagnostic.guard, "Guard") as factory:
            instance = factory.return_value
            instance.start.side_effect = guard.UserInput("real keyboard input (virtual key 0x41)")
            instance.diagnostics.return_value = {"probes_seen": 0}
            result = guard_diagnostic.diagnose(1)
        self.assertEqual(result["status"], "real_input")
        self.assertEqual(result["parent_desktop_context"]["window_station"], "WinSta0")
        self.assertNotIn("0x41", str(result))
        instance.stop.assert_called_once()

    def test_missing_liveness_probe_trips_without_reenabling_input(self):
        watcher = guard._HookObserver()
        with mock.patch.object(guard, "PROBE_INTERVAL", 0.001), \
             mock.patch.object(guard, "PROBE_TIMEOUT", 0.001), \
             mock.patch.object(watcher, "_send_probe", return_value=True) as send:
            watcher._watch()
        with self.assertRaises(guard.GuardUnavailable):
            watcher.check()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0] & 0xFFFFFFFF, guard.PROBE_TAG)
        self.assertEqual(watcher.diagnostics()["probes_seen"], 0)

    def test_first_probe_is_sent_before_the_interval(self):
        watcher = guard._HookObserver()

        def observed(_tag):
            watcher._probe_seen.set()
            return True

        with mock.patch.object(guard, "PROBE_INTERVAL", 10), \
             mock.patch.object(watcher, "_send_probe", side_effect=observed) as send:
            thread = guard.threading.Thread(target=watcher._watch)
            thread.start()
            try:
                self.assertTrue(watcher._first_probe_done.wait(0.2))
                send.assert_called_once()
            finally:
                watcher._stopping.set()
                thread.join(1)


class GuardProcessTests(unittest.TestCase):
    """The parent must not inject after the observer reports input or loses contact."""

    def setUp(self):
        self.parent, self.child = multiprocessing.Pipe()
        self.guard = guard.Guard()
        self.guard._connection = self.parent
        self.alive = True
        self.guard._process = SimpleNamespace(is_alive=lambda: self.alive)
        self.addCleanup(self.parent.close)
        self.addCleanup(self.child.close)

    def test_probe_diagnostics_do_not_mask_real_input(self):
        self.child.send(("ready", {"probes_seen": 21}))
        self.child.send(("user_input", "real keyboard input"))
        self.child.send(("diagnostics", {"probes_seen": 22}))
        with self.assertRaisesRegex(guard.UserInput, "real keyboard input"):
            self.guard.check()
        self.assertFalse(self.guard.unavailable)
        self.assertEqual(self.guard.diagnostics()["probes_seen"], 22)

    def test_missed_probe_is_fail_closed(self):
        self.child.send(("unavailable", "probe not observed"))
        with self.assertRaisesRegex(guard.GuardUnavailable, "probe not observed"):
            self.guard.check()
        self.assertTrue(self.guard.unavailable)

    def test_silent_observer_death_is_fail_closed(self):
        self.alive = False
        with self.assertRaisesRegex(guard.GuardUnavailable, "observer stopped"):
            self.guard.check()

    def test_pipe_loss_is_fail_closed_even_if_process_appears_alive(self):
        self.child.close()
        with self.assertRaisesRegex(guard.GuardUnavailable, "connection closed"):
            self.guard.check()

    def test_spawn_handshake_and_clean_stop(self):
        spawned = guard.Guard()
        with mock.patch.object(win32, "require_windows"), \
             mock.patch.object(guard, "_guard_worker", fake_observer_worker):
            try:
                spawned.start()
                self.assertIs(win32._before_input.__self__, spawned)
                spawned.check()
            finally:
                spawned.stop()
        self.assertIsNone(win32._before_input)
        self.assertEqual(spawned.diagnostics()["probes_seen"], 2)

    def test_diagnostic_flag_reaches_isolated_worker(self):
        spawned = guard.Guard(diagnostic=True)
        with mock.patch.object(win32, "require_windows"), \
             mock.patch.object(guard, "_guard_worker", fake_diagnostic_worker):
            try:
                spawned.start()
                self.assertTrue(spawned.diagnostics()["diagnostic"])
            finally:
                spawned.stop()
        self.assertEqual(spawned.diagnostics()["probes_seen"], 2)

    def test_real_input_before_first_probe_aborts_start(self):
        spawned = guard.Guard()
        with mock.patch.object(win32, "require_windows"), \
             mock.patch.object(guard, "_guard_worker", fake_early_input_worker):
            with self.assertRaisesRegex(guard.UserInput, "real keyboard input during startup"):
                spawned.start()
        self.assertIsNone(win32._before_input)

    def test_worker_preserves_real_input_during_initial_probe(self):
        class TouchedObserver:
            tripped = guard.threading.Event()
            reason = "real mouse button or wheel input"
            unavailable = False

            def start(self):
                self.tripped.set()
                raise RuntimeError("first probe interrupted")

            def stop(self):
                pass

            def diagnostics(self):
                return {"probes_seen": 0}

        with mock.patch.object(guard, "_HookObserver", TouchedObserver):
            guard._guard_worker(self.child)
        self.assertEqual(self.parent.recv(), ("user_input", "real mouse button or wheel input"))


class SettingsCheckTests(unittest.TestCase):
    def test_theme_cannot_pass_a_boot_error_page_that_changes_colour(self):
        from checks import settings as checks_settings
        ctx = SimpleNamespace(murmur=SimpleNamespace(ensure=lambda *_: None, show_settings=lambda: 42,
                                                     settings=lambda: "window"),
                              fixtures={"A": SimpleNamespace(path="fixture.wav")},
                              restorer=restore.Restorer(log=lambda _: None), sleep=lambda _: None)
        with mock.patch.object(win32, "Registry", return_value=SimpleNamespace(read=lambda *_: restore.MISSING)), \
             mock.patch.object(checks_settings.uia, "find", side_effect=checks_settings.uia.NotFound("General")):
            with self.assertRaises(checks_settings.uia.NotFound):
                checks_settings.s3_theme(ctx)

    def test_minimum_size_uses_user_resize_and_detects_undersized_client(self):
        from checks import settings as checks_settings
        ctx = SimpleNamespace(murmur=SimpleNamespace(ensure=lambda _: None, show_settings=lambda: 42),
                              fixtures={"A": SimpleNamespace(path="fixture.wav")},
                              restorer=restore.Restorer(log=lambda _: None), sleep=lambda _: None, note=lambda _: None,
                              screenshot=lambda *_: None)
        with mock.patch.object(win32, "outer_rect", return_value=(0, 0, 1016, 819)), \
             mock.patch.object(win32, "dpi_scale", return_value=1), \
             mock.patch.object(win32, "client_size", side_effect=[(1000, 780), (700, 500)]), \
             mock.patch.object(win32, "activate"), \
             mock.patch.object(win32, "drag") as drag, \
             mock.patch.object(win32, "set_window_pos") as set_pos:
            with self.assertRaisesRegex(Fail, "below the 760x560 px minimum"):
                checks_settings.s4_minimum_size(ctx)
            drag.assert_called_once_with((1012, 815), (496, 396))
            set_pos.assert_not_called()
            ctx.restorer.restore_all()
            set_pos.assert_called_once()


class GoldenDuplicateTests(unittest.TestCase):
    def test_delivered_twice_with_separator(self):
        message = golden.compare("Hello there.", "Hello there. Hello there.")
        self.assertIn("more than once", message)

    def test_prefix_extension_is_not_called_a_duplicate(self):
        message = golden.compare("Hello there.", "Hello there. And")
        self.assertNotIn("more than once", message)
        self.assertIn("differs at character 12", message)


class ChecksImportTests(unittest.TestCase):
    def test_every_check_module_imports_off_windows(self):
        import importlib
        for name in ("common", "components", "focus", "hotkeys", "insertion", "keyboard", "microphone",
                     "overlay", "settings", "setup", "tray"):
            importlib.import_module(f"checks.{name}")


class AbortedAggregationTests(unittest.TestCase):
    def test_an_aborted_repeat_is_not_hidden_by_an_earlier_pass(self):
        attempts = [report.Result("H1", report.PASS, "", "t", "g", attempt=1),
                    report.Result("H1", report.ABORTED, "aborted: user input (x)", "t", "g", attempt=2)]
        row = report.aggregate(attempts)
        self.assertEqual(row.status, report.ABORTED)
        self.assertIn("passed 1 earlier", row.reason)

    def test_a_failure_still_wins_over_an_abort(self):
        attempts = [report.Result("H1", report.FAIL, "boom", "t", "g", attempt=1),
                    report.Result("H1", report.ABORTED, "aborted: user input (x)", "t", "g", attempt=2)]
        self.assertEqual(report.aggregate(attempts).status, report.FAIL)


class DeliveredKeysTests(unittest.TestCase):
    def test_hotkey_modifier_presses_are_not_delivery(self):
        import targets
        keys = [{"char": "", "keysym": "Control_L", "state": 0, "t": 1.0},
                {"char": "", "keysym": "Shift_L", "state": 4, "t": 1.1},
                {"char": "\x04", "keysym": "d", "state": 12, "t": 1.2},
                {"char": "T", "keysym": "T", "state": 0, "t": 5.0},
                {"char": "h", "keysym": "h", "state": 0, "t": 5.1}]
        delivered = targets.delivered_keys(keys)
        self.assertEqual([k["char"] for k in delivered], ["T", "h"])
        self.assertEqual(delivered[0]["t"], 5.0)
