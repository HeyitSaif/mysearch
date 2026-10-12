"""jobs.heavy_work_allowed: the one gate for heavy background work (the whole-disk describer, the deep-folder queue).
The power source and the idle time are replaced; the thresholds and the reasons are checked at every boundary."""

import inspect
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import jobs  # noqa: E402


def gate(priority, ac, idle, enabled=True):
    with mock.patch("mse.jobs.on_ac_power_cached", return_value=ac), \
            mock.patch("mse.jobs.idle_seconds_cached", return_value=idle):
        return jobs.heavy_work_allowed(priority, enabled=enabled)


class LowPriorityTests(unittest.TestCase):
    def test_runs_only_on_ac_power_after_sixty_seconds_without_input(self):
        self.assertEqual(gate("low", True, 60.0), (True, None))
        self.assertEqual(gate("low", True, 3600.0), (True, None))

    def test_on_battery_is_on_battery_whatever_the_idle_time(self):
        self.assertEqual(gate("low", False, 600.0), (False, "on_battery"))
        self.assertEqual(gate("low", False, 0.0), (False, "on_battery"))

    def test_on_ac_but_active_is_user_active(self):
        self.assertEqual(gate("low", True, 0.0), (False, "user_active"))
        self.assertEqual(gate("low", True, 59.9), (False, "user_active"))


class BalancedPriorityTests(unittest.TestCase):
    def test_user_active_stops_it_at_once_on_ac_or_battery(self):
        self.assertEqual(gate("balanced", True, 4.9), (False, "user_active"))
        self.assertEqual(gate("balanced", False, 4.9), (False, "user_active"))
        self.assertEqual(gate("balanced", True, 0.0), (False, "user_active"))

    def test_on_ac_power_runs_after_five_seconds(self):
        self.assertEqual(gate("balanced", True, 5.0), (True, None))
        self.assertEqual(gate("balanced", True, 30.0), (True, None))

    def test_on_battery_needs_a_minute_without_input(self):
        self.assertEqual(gate("balanced", False, 59.9), (False, "on_battery"))
        self.assertEqual(gate("balanced", False, 60.0), (True, None))


class ReasonsAndSwitchesTests(unittest.TestCase):
    def test_disabled_wins_over_everything(self):
        for priority in ("low", "balanced"):
            for ac in (True, False):
                for idle in (0.0, 5.0, 600.0):
                    self.assertEqual(gate(priority, ac, idle, enabled=False), (False, "disabled"))

    def test_the_readings_are_not_taken_when_disabled(self):
        with mock.patch("mse.jobs.on_ac_power_cached") as ac, mock.patch("mse.jobs.idle_seconds_cached") as idle:
            jobs.heavy_work_allowed("low", enabled=False)
            ac.assert_not_called()
            idle.assert_not_called()

    def test_an_unknown_priority_is_an_error(self):
        for bad in ("", "fast", "LOW", None):
            with self.subTest(priority=bad), self.assertRaises(ValueError):
                jobs.heavy_work_allowed(bad)

    def test_the_default_is_enabled_and_the_call_takes_the_priority_alone(self):
        with mock.patch("mse.jobs.on_ac_power_cached", return_value=True), \
                mock.patch("mse.jobs.idle_seconds_cached", return_value=120.0):
            self.assertEqual(jobs.heavy_work_allowed("low"), (True, None))

    def test_the_result_is_always_a_pair(self):
        for priority in ("low", "balanced"):
            for ac in (True, False):
                for idle in (0.0, 4.0, 59.0, 600.0):
                    allowed, reason = gate(priority, ac, idle)
                    self.assertIsInstance(allowed, bool)
                    self.assertEqual(allowed, reason is None)
                    self.assertIn(reason, (None, "user_active", "on_battery", "disabled"))


class ParityTests(unittest.TestCase):
    """The whole-disk describer's gate is this function: the same answer on a grid of readings."""

    def test_the_describer_answers_exactly_as_the_shared_gate(self):
        from mse import wholedisk
        m = wholedisk.Manager(db=os.path.join(tempfile.mkdtemp(prefix="gate-"), "wd.db"))
        for priority in ("low", "balanced"):
            for ac in (True, False):
                for idle in (0.0, 4.9, 5.0, 59.9, 60.0, 900.0):
                    with mock.patch("mse.jobs.on_ac_power_cached", return_value=ac), \
                            mock.patch("mse.jobs.idle_seconds_cached", return_value=idle):
                        self.assertEqual(m.gate({"enabled": True, "priority": priority}),
                                         jobs.heavy_work_allowed(priority))


class IndependenceTests(unittest.TestCase):
    def test_jobs_holds_no_whole_disk_or_feature_state(self):
        src = inspect.getsource(jobs.heavy_work_allowed)
        self.assertNotIn("wholedisk", src)
        self.assertNotIn("config", src)
        self.assertNotIn("store", src)


if __name__ == "__main__":
    unittest.main()
