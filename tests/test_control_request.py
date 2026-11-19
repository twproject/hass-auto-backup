"""Original service-field and GUI request contracts, independent of HA."""

import importlib
import os
from pathlib import Path
import sys
import types
import unittest
from datetime import datetime, timezone

PACKAGE = "complete_request_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [
    os.environ.get(
        "AUTO_BACKUP_SOURCE",
        str(Path(__file__).parents[1] / "custom_components/auto_backup"),
    )
]
sys.modules[PACKAGE] = package
request = importlib.import_module(PACKAGE + ".control_request")
policy = importlib.import_module(PACKAGE + ".control_policy")


class RequestTests(unittest.TestCase):
    def test_all_automatic_include_exclude_formats_normalize_equally(self):
        common = {
            "include_folders": ["config"],
            "include_addons": ["Z-Wave JS UI", "core_*"],
            "exclude_folders": ["ssl"],
            "exclude_addons": ["old_*"],
        }
        r = request.validate_request({"service": "backup", "data": common})
        self.assertEqual(
            r["data"],
            {
                "include": {
                    "folders": ["homeassistant"],
                    "addons": ["Z-Wave JS UI", "core_*"],
                },
                "exclude": {"folders": ["ssl"], "addons": ["old_*"]},
            },
        )

    def test_full_and_partial_have_original_service_semantics(self):
        self.assertEqual(
            request.manager_payload(
                request.validate_request(
                    {
                        "service": "backup_partial",
                        "data": {"folders": ["media"], "addons": []},
                    }
                )
            ),
            {"include": {"folders": ["media"], "addons": []}},
        )
        with self.assertRaises(ValueError):
            request.validate_request(
                {"service": "backup_full", "data": {"include_folders": ["media"]}}
            )

    def test_all_common_options_reach_the_backup_unchanged(self):
        data = {
            "name": "Manual",
            "password": "TEST ONLY",
            "keep_days": 0.5,
            "download_path": ["/tmp"],
            "compressed": False,
            "location": "test_mount",
            "encrypted": False,
            "exclude_database": True,
        }
        self.assertEqual(
            request.validate_request({"service": "backup_full", "data": data})["data"],
            data,
        )

    def test_no_configuration_folder_and_automatic_default_name_supported(self):
        p = policy.validate_policy({"name": "", "exclude_folders": ["homeassistant"]})
        r = request.policy_request(p, {}, datetime.now(timezone.utc))
        self.assertEqual(r["data"]["name"], "")
        self.assertEqual(r["data"]["exclude"]["folders"], ["homeassistant"])

    def test_old_encryption_setting_migrates_without_changing_mode(self):
        p = policy.validate_policy({"encrypted": True})
        self.assertEqual(p["encryption"], "default")
        self.assertTrue(
            request.policy_request(p, {}, datetime.now(timezone.utc))["data"][
                "encrypted"
            ]
        )

    def test_saved_custom_password_does_not_fall_back_to_recovery_key(self):
        p = policy.validate_policy({"encryption": "password"})
        with self.assertRaises(ValueError):
            request.policy_request(p, {}, datetime.now(timezone.utc))
        r = request.policy_request(
            p, {"backup_password": "private"}, datetime.now(timezone.utc)
        )
        self.assertEqual(r["data"]["password"], "private")
        self.assertFalse(r["data"]["encrypted"])

    def test_nas_conflicts_rejected_instead_of_silently_ignored(self):
        for data in ({"keep_days": 7}, {"download_path": ["/tmp"]}):
            with self.assertRaises(ValueError):
                request.check_capabilities({"data": data}, True, True)
        request.check_capabilities({"data": {"keep_days": 7}}, False, True)

    def test_core_full_backup_does_not_require_nas_or_supervisor(self):
        request.check_capabilities({"data": {"encrypted": False}}, False, False)
        for data in (
            {"compressed": False},
            {"location": "nas"},
            {"exclude": {"folders": ["media"]}},
        ):
            with self.assertRaises(ValueError):
                request.check_capabilities({"data": data}, False, False)

    def test_expiry_rejects_non_finite_or_boolean_numbers(self):
        for value in (float("nan"), float("inf"), True, -1):
            with self.assertRaises(ValueError):
                request.validate_request(
                    {"service": "backup", "data": {"keep_days": value}}
                )

    def test_download_directory_must_exist(self):
        with self.assertRaises(ValueError):
            request.check_directories(
                {"data": {"download_path": ["/path/that/cannot/exist/test"]}}
            )

    def test_unexpected_fields_and_ambiguous_aliases_rejected(self):
        for data in ({"execute": "command"}, {"include": {}, "include_folders": []}):
            with self.assertRaises(ValueError):
                request.validate_request({"service": "backup", "data": data})


if __name__ == "__main__":
    unittest.main()
