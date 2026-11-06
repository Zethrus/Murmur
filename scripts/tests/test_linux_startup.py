import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("startup", Path(__file__).parents[1] / "test-linux-startup.py")
startup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup)


class StartupTimeoutTests(unittest.TestCase):
    def test_slow_appimage_launch_can_finish_after_old_deadline(self):
        app = Mock()
        app.poll.return_value = None
        condition = Mock(side_effect=[False, True])
        with patch.object(startup.time, "monotonic", side_effect=[0, 1, 31]), patch.object(startup.time, "sleep"):
            startup.wait_for(condition, app, "not ready", startup.launch_timeout(Path("Murmur.AppImage")))
        self.assertEqual(condition.call_count, 2)

    def test_extracted_runtime_retains_short_deadline(self):
        app = Mock()
        app.poll.return_value = None
        with patch.object(startup.time, "monotonic", side_effect=[0, 26]):
            with self.assertRaisesRegex(AssertionError, "not ready"):
                startup.wait_for(lambda: False, app, "not ready", startup.launch_timeout(Path("AppRun")))

    def test_appimage_timeout_is_still_bounded(self):
        with patch.object(startup.time, "monotonic", side_effect=[0, 181]):
            with self.assertRaisesRegex(AssertionError, "not ready"):
                startup.wait_for(lambda: False, Mock(), "not ready", startup.launch_timeout(Path("Murmur.AppImage")))

    def test_crash_is_reported_without_waiting_for_deadline(self):
        app = Mock(returncode=1)
        app.poll.return_value = 1
        with self.assertRaisesRegex(AssertionError, "exited early: 1"):
            startup.wait_for(lambda: False, app, "not ready", 180)
