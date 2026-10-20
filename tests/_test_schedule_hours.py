"""Configurable background schedule hours (#456), defaults unchanged."""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_scheduler
import wb_settings


class StubPool(object):
    def __init__(self, directory):
        self.dir = directory
        self.accounts = []


class ScheduleSettingsTests(unittest.TestCase):
    def test_defaults_when_the_key_is_absent(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            self.assertEqual(wb_settings.schedule_hours(directory), {
                "checkin": [9, 21], "travel": [9, 21],
                "keepalive": [22], "cat": [1],
            })

    def test_list_and_comma_string_are_accepted(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": "20, 10,10", "travel": [6, 18],
                "keepalive": "12", "cat": [1],
            })
            hours = wb_settings.schedule_hours(directory)
            self.assertEqual(hours["checkin"], [10, 20])
            self.assertEqual(hours["travel"], [6, 18])
            self.assertEqual(hours["keepalive"], [12])

    def test_invalid_empty_and_out_of_range_fall_back_per_key(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            data = wb_settings.load(directory)
            data["schedule"] = {
                "checkin": [24, -1, "x"],   # nothing valid -> default
                "travel": [],                 # empty -> default
                "keepalive": "99",            # out of range -> default
                "cat": [3, 3],                # valid -> kept
            }
            wb_settings.save(directory, data)
            hours = wb_settings.schedule_hours(directory)
            self.assertEqual(hours["checkin"], [9, 21])
            self.assertEqual(hours["travel"], [9, 21])
            self.assertEqual(hours["keepalive"], [22])
            self.assertEqual(hours["cat"], [3])

    def test_partial_update_keeps_other_keys(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            wb_settings.set_schedule_hours(directory, {"checkin": [10, 20]})
            wb_settings.set_schedule_hours(directory, {"keepalive": [12]})
            hours = wb_settings.schedule_hours(directory)
            self.assertEqual(hours["checkin"], [10, 20])
            self.assertEqual(hours["keepalive"], [12])
            self.assertEqual(hours["travel"], [9, 21])

    def test_invalid_write_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            with self.assertRaises(ValueError):
                wb_settings.set_schedule_hours(directory, {"checkin": []})
            with self.assertRaises(ValueError):
                wb_settings.set_schedule_hours(directory, {"cat": "26"})
            with self.assertRaises(ValueError):
                wb_settings.set_schedule_hours(directory, "9,21")


class SchedulerScheduleTests(unittest.TestCase):
    def test_scheduler_picks_up_changes_without_restart(self):
        with tempfile.TemporaryDirectory(prefix="sched-run-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [10, 20],
                "keepalive": [12], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            self.assertEqual(scheduler.checkin_hours, [10, 20])
            self.assertEqual(scheduler.keepalive_hours, [12])
            self.assertEqual(scheduler.all_hours, [1, 10, 12, 20])
            self.assertIn("10:00/20:00", scheduler.status()["mode_cn"])
            self.assertIn("12:00", scheduler.status()["mode_intl"])

            wb_settings.set_schedule_hours(directory, {"checkin": [8]})
            self.assertTrue(scheduler._reload_schedule())
            self.assertEqual(scheduler.checkin_hours, [8])
            self.assertIn("08:00", scheduler.status()["mode_cn"])


class FakeAccount(object):
    def __init__(self, realm="cn"):
        self.uid = "fake-uid-1234"
        self.realm = realm
        self.restricted_until = 0.0
        self.expires_at = 0.0
        self.last_error = ""
        self.checkins = 0
        self.reports = 0
        self.daily = 0
        self.refreshes = 0

    def can_checkin(self):
        return True

    def checkin(self, trigger=""):
        self.checkins += 1
        return {"ok": True, "msg": "OK"}

    def can_report_activity(self):
        return True

    def report_activity(self):
        self.reports += 1
        return {"ok": True, "msg": "OK"}

    def can_daily_chat(self):
        return True

    def daily_chat(self, trigger=""):
        self.daily += 1
        return {"ok": True, "msg": "OK"}

    def refresh(self):
        self.refreshes += 1
        return True


class TaskGatingTests(unittest.TestCase):
    """A configured hour means that task, not "run the whole routine".

    #515: the 01:00 cat slot used to run check-in, activity report and cat
    travel as well, so a schedule of 10:00/20:00 still checked in at 01:00.
    """

    def _run_at(self, scheduler, account, hour, force=False):
        stamp = time.struct_time((2026, 10, 20, hour, 0, 0, 0, 0, -1))
        seen = {"travel": 0, "bonus": 0, "cat": 0}

        def travel(_acc):
            seen["travel"] += 1
            return {}

        def bonus(_acc):
            seen["bonus"] += 1
            return {}

        def night(_acc):
            seen["cat"] += 1
            return {"logs": []}

        with mock.patch.object(wb_scheduler.time, "localtime", lambda *a: stamp), \
                mock.patch.object(wb_scheduler.time, "sleep", lambda *_: None), \
                mock.patch.object(wb_scheduler, "do_cat_travel", travel), \
                mock.patch.object(wb_scheduler.wb_tasks, "run_streak_bonus", bonus), \
                mock.patch.object(wb_scheduler.wb_tasks, "run_night_growth", night):
            scheduler._run_cycle("test", force=force)
        return seen

    def test_cat_hour_does_not_run_checkin_or_travel(self):
        with tempfile.TemporaryDirectory(prefix="sched-gate-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [10, 20],
                "keepalive": [12], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            account = FakeAccount()
            scheduler.pool.accounts = [account]

            seen = self._run_at(scheduler, account, 1)
            self.assertEqual((account.checkins, account.reports), (0, 0))
            self.assertEqual((seen["travel"], seen["bonus"], seen["cat"]), (0, 0, 1))

    def test_checkin_hour_runs_checkin_and_report_but_not_travel(self):
        with tempfile.TemporaryDirectory(prefix="sched-gate-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [9, 21],
                "keepalive": [12], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            account = FakeAccount()
            scheduler.pool.accounts = [account]

            seen = self._run_at(scheduler, account, 10)
            self.assertEqual((account.checkins, account.reports), (1, 1))
            self.assertEqual((seen["travel"], seen["bonus"], seen["cat"]), (0, 0, 0))

    def test_manual_trigger_forces_every_task(self):
        with tempfile.TemporaryDirectory(prefix="sched-gate-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [10, 20],
                "keepalive": [12], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            account = FakeAccount()
            scheduler.pool.accounts = [account]

            seen = self._run_at(scheduler, account, 3, force=True)
            self.assertEqual((account.checkins, account.reports), (1, 1))
            self.assertEqual((seen["travel"], seen["bonus"], seen["cat"]), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
