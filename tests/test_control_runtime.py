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
            async_add_executor_job=AsyncMock(side_effect=lambda fn, *args: fn(*args)),
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
            purge_backups=AsyncMock(),
            get_purgeable_snapshots=lambda: [],
            _handler=SimpleNamespace(get_backups=AsyncMock(return_value=[])),
            monitored=0,
            purgeable=0,
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

    async def test_backup_without_nas_and_partial_payload(self):
        ctl = self.controller()
        ctl.manager._nas.enabled = False
        ctl.launch(
            "backup",
            "test",
            request={
                "service": "backup_partial",
                "data": {
                    "folders": ["media"],
                    "addons": [],
                    "compressed": False,
                    "keep_days": 0.5,
                },
            },
        )
        await ctl.task
        data = ctl.manager.async_create_backup.call_args.args[0]
        self.assertEqual(data["include"], {"folders": ["media"], "addons": []})
        self.assertEqual(data["keep_days"], 0.5)
        self.assertFalse(data["compressed"])
        self.assertEqual(ctl.history[-1]["status"], "success")

    async def test_custom_password_not_returned_or_written_to_history(self):
        ctl = self.controller({"backup_password": "secret-value"})
        ctl.launch(
            "backup",
            "test",
            request={"service": "backup_full", "data": {"password": "one-shot-secret"}},
        )
        await ctl.task
        self.assertNotIn("secret-value", str(ctl.state()))
        self.assertNotIn("one-shot-secret", str(ctl.history))

    async def test_custom_password_clear_is_explicit_and_blank_preserves(self):
        current = {"backup_password": "saved"}
        self.assertEqual(
            validate_options(current, {"backup_password": ""})["backup_password"],
            "saved",
        )
        self.assertNotIn(
            "backup_password",
            validate_options(current, {"clear_backup_password": True}),
        )

    async def test_native_cleanup_checks_preview_again_before_deletion(self):
        ctl = self.controller()
        ctl.manager._nas.enabled = False
        ctl.manager.get_purgeable_snapshots = lambda: ["expired1"]
        preview = await ctl.purge_preview()
        ctl.manager.get_purgeable_snapshots = lambda: ["expired1", "expired2"]
        ctl.launch("purge", "test", confirmation=preview["confirmation"])
        await ctl.task
        ctl.manager.purge_backups.assert_not_awaited()
        self.assertEqual(ctl.history[-1]["status"], "error")

    async def test_native_cleanup_requires_confirmation_and_uses_manager(self):
        ctl = self.controller()
        ctl.manager._nas.enabled = False
        with self.assertRaises(ValueError):
            ctl.launch("purge", "test")
        preview = await ctl.purge_preview()
        ctl.launch("purge", "test", confirmation=preview["confirmation"])
        await ctl.task
        ctl.manager.purge_backups.assert_awaited_once()

    async def test_standard_mode_skips_only_nas_verification_slot(self):
        ctl = self.controller({"policy": {"enabled": True}})
        ctl.manager._nas.enabled = False
        self.assertFalse(ctl.effective_schedule()["verify_enabled"])
        self.assertTrue(ctl.effective_schedule()["report_enabled"])
        ctl.launch("report", "test")
        await ctl.task
        ctl.manager._nas.check.assert_not_awaited()
        self.assertEqual(ctl.history[-1]["status"], "success")

    async def test_gui_request_fields_cover_every_original_creation_schema(self):
        import voluptuous as vol
        from custom_components.auto_backup import MAP_SERVICES
        from custom_components.auto_backup.control_request import SERVICE_FIELDS

        def keys(schema):
            if isinstance(schema, vol.Schema):
                return {getattr(k, "schema", k) for k in schema.schema}
            return set().union(*(keys(v) for v in schema.validators))

        for service, fields in SERVICE_FIELDS.items():
            self.assertEqual(fields, keys(MAP_SERVICES[service]), service)

    async def test_core_inventory_uses_local_agent_metadata(self):
        from custom_components.auto_backup.handlers import BackupHandler

        item = SimpleNamespace(
            name="Backup",
            date="2026-11-19T10:00:00Z",
            agents={"local": SimpleNamespace(size=123, protected=True)},
        )
        remote = SimpleNamespace(
            name="Remote only",
            date="2026-11-19",
            agents={"remote": SimpleNamespace(size=999, protected=False)},
        )
        native = SimpleNamespace(
            async_get_backups=AsyncMock(return_value=({"a": item, "b": remote}, {})),
            local_backup_agents={"local": object()},
        )
        handler = BackupHandler(None, native)
        rows = await handler.get_backups()
        self.assertEqual(
            rows,
            [
                {
                    "slug": "a",
                    "name": "Backup",
                    "date": "2026-11-19T10:00:00Z",
                    "size": 123,
                    "protected": True,
                }
            ],
        )

    async def test_core_local_agent_failure_is_not_an_empty_inventory(self):
        from custom_components.auto_backup.handlers import BackupHandler, HassioAPIError

        native = SimpleNamespace(
            async_get_backups=AsyncMock(
                return_value=({"remote": object()}, {"local": RuntimeError()})
            ),
            local_backup_agents={"local": object()},
        )
        with self.assertRaises(HassioAPIError):
            await BackupHandler(None, native).get_backups()

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
