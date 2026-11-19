"""Calendar and input contract, runnable without Home Assistant."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import importlib
import os
from pathlib import Path
import sys
import types
import unittest

PACKAGE = "control_policy_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [
    os.environ.get(
        "AUTO_BACKUP_SOURCE",
        str(Path(__file__).parents[1] / "custom_components/auto_backup"),
    )
]
sys.modules[PACKAGE] = package
policy = importlib.import_module(PACKAGE + ".control_policy")


class PolicyTests(unittest.TestCase):
    def test_defaults_do_not_activate_schedule(self):
        self.assertFalse(policy.validate_policy({})["enabled"])
        self.assertEqual(
            policy.due_slots(
                policy.validate_policy({}), datetime.now(timezone.utc), {}
            ),
            [],
        )

    def test_claimed_date_not_repeated_after_restart(self):
        p = policy.validate_policy({"enabled": True})
        now = datetime(2026, 11, 19, 10, tzinfo=timezone.utc)
        self.assertEqual([s[0] for s in policy.due_slots(p, now, {})], ["backup"])
        self.assertEqual(policy.due_slots(p, now, {"backup:2026-11-19": "claimed"}), [])

    def test_selected_days_and_catchup_window(self):
        p = policy.validate_policy(
            {"enabled": True, "backup_days": [0], "catchup_hours": 1}
        )
        self.assertEqual(
            policy.due_slots(p, datetime(2026, 11, 19, 10, tzinfo=timezone.utc), {}), []
        )
        self.assertEqual(
            policy.due_slots(p, datetime(2026, 11, 23, 11, tzinfo=timezone.utc), {}), []
        )
        self.assertEqual(
            len(
                policy.due_slots(p, datetime(2026, 11, 23, 10, tzinfo=timezone.utc), {})
            ),
            1,
        )

    def test_dst_fold_runs_once_per_local_date(self):
        tz = ZoneInfo("Europe/Rome")
        p = policy.validate_policy(
            {
                "enabled": True,
                "backup_time": "02:30",
                "verify_enabled": False,
                "report_enabled": False,
            }
        )
        now = datetime(2026, 10, 25, 2, 40, tzinfo=tz, fold=1)
        slots = policy.due_slots(p, now, {})
        self.assertEqual(len(slots), 1)
        self.assertEqual(policy.due_slots(p, now, {slots[0][1]: "claimed"}), [])

    def test_dst_gap_moves_to_valid_time(self):
        tz = ZoneInfo("Europe/Rome")
        p = policy.validate_policy(
            {
                "enabled": True,
                "backup_time": "02:30",
                "verify_enabled": False,
                "report_enabled": False,
            }
        )
        now = datetime(2026, 3, 29, 3, 40, tzinfo=tz)
        slots = policy.due_slots(p, now, {})
        self.assertEqual(slots[0][2].hour, 3)

    def test_three_sunday_operations_have_separate_claims(self):
        p = policy.validate_policy({"enabled": True})
        slots = policy.due_slots(p, datetime(2026, 11, 22, 13, tzinfo=timezone.utc), {})
        self.assertEqual({s[0] for s in slots}, {"backup", "verify", "report"})

    def test_next_schedule_crosses_week(self):
        p = policy.validate_policy({"enabled": True, "backup_days": [0]})
        next_ = policy.next_slots(
            p, datetime(2026, 11, 23, 10, tzinfo=timezone.utc), {}
        )
        self.assertTrue(next_["backup"].startswith("2026-11-30"))

    def test_invalid_options_rejected(self):
        for value in (
            {"enabled": "false"},
            {"backup_days": []},
            {"backup_days": [True]},
            {"backup_days": [7]},
            {"backup_time": "24:00"},
            {"verify_time": "3:00"},
            {"catchup_hours": 24},
            {"notify_entities": ["script.unsafe"]},
            {"exclude_addons": ["../all"]},
            {"unknown": 1},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.validate_policy(value)

    def test_defaults_are_not_shared(self):
        first = policy.validate_policy({})
        first["backup_days"].clear()
        self.assertEqual(len(policy.validate_policy({})["backup_days"]), 7)

    def test_zero_catchup_allows_entire_scheduled_minute(self):
        p = policy.validate_policy({"enabled": True, "catchup_hours": 0})
        now = datetime(2026, 11, 19, 9, 53, 59, 999999, tzinfo=timezone.utc)
        self.assertEqual(len(policy.due_slots(p, now, {})), 1)
        later = datetime(2026, 11, 19, 9, 54, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(policy.due_slots(p, later, {}), [])


if __name__ == "__main__":
    unittest.main()
