"""Controller failure/restart contracts; run using HA's Python environment."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch, Mock

from custom_components.auto_backup.control import BackupControl
from custom_components.auto_backup.control_panel import validate_options, command
from custom_components.auto_backup.nas_manager import NasBackupManager
from homeassistant.exceptions import Unauthorized


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, options=None):
        hass = SimpleNamespace(
            config=SimpleNamespace(time_zone="Europe/Rome"),
            bus=SimpleNamespace(async_fire=lambda *a: None),
            services=SimpleNamespace(async_call=AsyncMock()),
            async_create_task=asyncio.create_task,
        )
        manager = SimpleNamespace(
            _backup_lock=asyncio.Lock(),
            _nas=SimpleNamespace(
                lock=asyncio.Lock(),
                enabled=True,
                receipts={},
                verify=AsyncMock(),
                check=AsyncMock(),
            ),
            nas_status={},
            async_create_backup=AsyncMock(return_value=True),
        )
        with patch("custom_components.auto_backup.control.Store"):
            ctl = BackupControl(hass, SimpleNamespace(options=options or {}), manager)
        ctl.store = SimpleNamespace(
            async_load=AsyncMock(return_value={}), async_save=AsyncMock()
        )
        return ctl

    async def test_failed_backup_is_logged_not_success(self):
        ctl = self.controller()
        ctl.manager.async_create_backup.return_value = False
        await ctl.run("backup", "test")
        self.assertEqual(ctl.history[0]["status"], "error")
        ctl.hass.services.async_call.assert_awaited()

    async def test_running_operation_rejects_second_request(self):
        ctl = self.controller()
        gate = asyncio.Event()

        async def wait_for_gate(_):
            await gate.wait()
            return True

        ctl.manager.async_create_backup.side_effect = wait_for_gate
        ctl.launch("backup", "test")
        with self.assertRaises(Exception):
            ctl.launch("verify", "test")
        gate.set()
        await ctl.task

    async def test_claim_is_durable_before_launch(self):
        ctl = self.controller({"policy": {"enabled": True}})
        events = []
        ctl.store.async_save.side_effect = lambda _: events.append("saved")
        ctl.launch = lambda *args: events.append("launched")
        now = datetime(2026, 11, 19, 10, tzinfo=timezone.utc)
        with patch(
            "custom_components.auto_backup.control.dt_util.now", return_value=now
        ):
            await ctl.tick(None)
            await ctl.tick(None)
        self.assertEqual(events[:2], ["saved", "launched"])
        self.assertEqual(events.count("launched"), 1)

    async def test_periodic_nas_error_notifies_even_with_recent_backup(self):
        ctl = self.controller({"policy": {"enabled": True, "backup_time": "23:59"}})
        ctl.manager.nas_status = {
            "status": "error",
            "last_verified": "2026-11-19T10:00:00+00:00",
        }
        with patch(
            "custom_components.auto_backup.control.dt_util.now",
            return_value=datetime(2026, 11, 19, 10, tzinfo=timezone.utc),
        ):
            await ctl.tick(None)
        ctl.hass.services.async_call.assert_not_awaited()
        with patch(
            "custom_components.auto_backup.control.dt_util.now",
            return_value=datetime(2026, 11, 19, 10, 16, tzinfo=timezone.utc),
        ):
            await ctl.tick(None)
            await ctl.tick(None)
        self.assertEqual(ctl.hass.services.async_call.await_count, 1)

    async def test_nas_recovery_resets_pending_alert(self):
        ctl = self.controller({"policy": {"enabled": True, "backup_time": "23:59"}})
        ctl.alerts["nas_problem_since"] = "2026-11-19T10:00:00+00:00"
        ctl.manager.nas_status = {
            "status": "ok",
            "last_verified": "2026-11-19T10:00:00+00:00",
        }
        with patch(
            "custom_components.auto_backup.control.dt_util.now",
            return_value=datetime(2026, 11, 19, 10, 16, tzinfo=timezone.utc),
        ):
            await ctl.tick(None)
        self.assertNotIn("nas_problem_since", ctl.alerts)
        ctl.hass.services.async_call.assert_not_awaited()

    async def test_disabled_error_notifications_do_not_report_nas_problem(self):
        ctl = self.controller(
            {
                "policy": {
                    "enabled": True,
                    "notify_errors": False,
                    "backup_time": "23:59",
                }
            }
        )
        ctl.alerts["nas_problem_since"] = "2026-11-19T10:00:00+00:00"
        ctl.manager.nas_status = {"status": "error"}
        with patch(
            "custom_components.auto_backup.control.dt_util.now",
            return_value=datetime(2026, 11, 19, 10, 16, tzinfo=timezone.utc),
        ):
            await ctl.tick(None)
        ctl.hass.services.async_call.assert_not_awaited()

    async def test_secrets_never_returned_in_state(self):
        ctl = self.controller({"nas_password": "private-test-password"})
        self.assertNotIn("private-test-password", str(ctl.state()))
        self.assertTrue(ctl.state()["options"]["nas_password_set"])

    async def test_options_preserve_password_and_policy(self):
        old = {"nas_password": "saved", "policy": {"enabled": False}}
        new = validate_options(old, {"nas_password": "", "nas_daily": 7})
        self.assertEqual(new["nas_password"], "saved")
        self.assertFalse(new["policy"]["enabled"])

    async def test_path_traversal_and_unknown_settings_rejected(self):
        for changes in (
            {"anything": True},
            {
                "nas_enabled": True,
                "nas_server": "nas",
                "nas_share": "backup",
                "nas_folder": "../production",
            },
            {"backup_timeout": True},
        ):
            with self.assertRaises(
                ValueError if "nas_folder" not in changes else Exception
            ):
                validate_options({}, changes)

    async def test_notification_failure_keeps_persistent_warning(self):
        ctl = self.controller({"policy": {"notify_entities": ["notify.test"]}})
        ctl.hass.services.async_call.side_effect = [
            RuntimeError("endpoint unavailable"),
            None,
        ]
        await ctl.notify("message")
        self.assertEqual(
            ctl.hass.services.async_call.call_args.args[0], "persistent_notification"
        )

    async def test_non_admin_api_request_rejected_before_data_access(self):
        connection = SimpleNamespace(
            user=SimpleNamespace(is_admin=False), send_error=Mock()
        )
        with self.assertRaises(Unauthorized):
            command(
                SimpleNamespace(),
                connection,
                {"id": 12, "type": "auto_backup/control", "action": "state"},
            )

    async def test_destination_changes_keep_separate_receipts(self):
        hass = SimpleNamespace(bus=SimpleNamespace(async_fire=lambda *a: None))
        with patch("custom_components.auto_backup.nas_manager.Store"):
            nas = NasBackupManager(
                hass,
                {"nas_server": "a", "nas_share": "test", "nas_folder": "first"},
                None,
            )
        nas.store = SimpleNamespace(async_save=AsyncMock())
        nas.namespace = "abcdef123456"
        nas.receipts = {"old.tar": {"slug": "01234567"}}
        nas.start = lambda: None
        await nas.update_options(
            {"nas_server": "a", "nas_share": "test", "nas_folder": "second"}
        )
        self.assertEqual(nas.receipts, {})
        await nas.update_options(
            {"nas_server": "a", "nas_share": "test", "nas_folder": "first"}
        )
        self.assertIn("old.tar", nas.receipts)

    async def test_missing_file_is_not_listed_as_present(self):
        ctl = self.controller()
        ctl.manager._nas.catalog = []
        ctl.manager._nas.receipts = {"missing.tar": {"sha256": "abc"}}
        self.assertFalse(ctl.state()["copies"][0]["present"])


if __name__ == "__main__":
    unittest.main()
