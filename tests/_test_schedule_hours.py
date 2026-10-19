"""Configurable background schedule hours (#456), defaults unchanged."""
import os
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
