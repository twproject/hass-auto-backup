"""Admin-only WebSocket API and self-contained HA sidebar panel."""

from pathlib import Path

import voluptuous as vol
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.hassio import is_hassio
from homeassistant.util import dt as dt_util

from .const import DATA_AUTO_BACKUP, DOMAIN, DEFAULT_BACKUP_TIMEOUT
from .control_policy import validate_policy
from .nas_manager import config_from_options
from . import nas_backend

SETTINGS = {
    "auto_purge",
    "backup_timeout",
    "nas_enabled",
    "nas_server",
    "nas_share",
    "nas_folder",
    "nas_username",
    "nas_password",
    "nas_workgroup",
    "nas_daily",
    "nas_weekly",
    "nas_monthly",
    "nas_keep_local",
    "nas_manage_legacy",
    "policy",
}


def validate_options(current, changes):
    if not isinstance(changes, dict) or set(changes) - SETTINGS:
        raise ValueError("Unknown setting")
    result = {
        "auto_purge": True,
        "backup_timeout": DEFAULT_BACKUP_TIMEOUT,
        **current,
        **changes,
    }
    if "nas_password" in changes and not changes["nas_password"]:
        result["nas_password"] = current.get("nas_password", "")
    for key in ("auto_purge", "nas_enabled", "nas_manage_legacy"):
        if type(result.get(key, False)) is not bool:
            raise ValueError("Invalid switch: " + key)
    if (
        type(result["backup_timeout"]) is not int
        or not 5 <= result["backup_timeout"] <= 360
    ):
        raise ValueError("Backup timeout must be between 5 and 360 minutes")
    for key in (
        "nas_server",
        "nas_share",
        "nas_folder",
        "nas_username",
        "nas_workgroup",
        "nas_password",
    ):
        if not isinstance(result.get(key, ""), str) or len(result.get(key, "")) > 512:
            raise ValueError("Invalid NAS field")
    result["policy"] = validate_policy(result.get("policy", {}))
    if result.get("nas_enabled") or result["policy"]["enabled"]:
        nas_backend.validate_config(config_from_options(result))
    if result["policy"]["enabled"] and not result.get("nas_enabled"):
        raise ValueError("Enable NAS before scheduling")
    return result


@websocket_api.websocket_command(
    {
        vol.Required("type"): "auto_backup/control",
        vol.Required("action"): vol.In(
            ["state", "save", "test_connection", "backup", "verify", "check", "report"]
        ),
        vol.Optional("options"): dict,
        vol.Optional("revision"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def command(hass, connection, msg):
    manager = hass.data.get(DATA_AUTO_BACKUP)
    control = getattr(manager, "_control", None)
    if not control:
        connection.send_error(msg["id"], "not_loaded", "Auto Backup is not loaded")
        return
    try:
        action = msg["action"]
        if action in ("save", "test_connection"):
            if control.busy or (control.task and not control.task.done()):
                raise ValueError(
                    "Wait for the running operation before changing settings"
                )
            options = validate_options(
                dict(control.entry.options), msg.get("options", {})
            )
            if options.get("nas_enabled") and not is_hassio(hass):
                raise ValueError("NAS mode requires Supervisor")
            if action == "test_connection":
                # Read-only probe of the proposed destination; no settings changed.
                result = await hass.async_add_executor_job(
                    nas_backend.scan,
                    config_from_options(options),
                    manager._nas.namespace,
                    dt_util.now(),
                )
                connection.send_result(
                    msg["id"],
                    {
                        "connected": True,
                        "status": result["status"],
                        "copies": result["copies"],
                    },
                )
                return
            if msg.get("revision") != control.revision:
                raise ValueError("Settings changed elsewhere. Refresh before saving")
            if options["policy"]["enabled"] and options.get("nas_manage_legacy"):
                raise ValueError(
                    "Disable legacy adoption before enabling the built-in schedule"
                )
            known = {s.entity_id for s in hass.states.async_all("notify")}
            if set(options["policy"]["notify_entities"]) - known:
                raise ValueError("A selected notification target is unavailable")
            if (
                options["policy"]["encrypted"]
                and not manager._manager.config.data.create_backup.password
            ):
                raise ValueError(
                    "Configure the recovery key in Home Assistant Backup settings before enabling encryption"
                )
            hass.config_entries.async_update_entry(control.entry, options=options)
            await manager.update_listener(hass, control.entry)
            control.changed()
        elif action != "state":
            control.launch(action, connection.user.id)
        result = control.state()
        if action == "state" and hasattr(manager._handler, "get_backups"):
            try:
                local = {item["slug"] for item in await manager._handler.get_backups()}
                for item in result["copies"]:
                    item["local_present"] = item.get("slug") in local
            except (HomeAssistantError, RuntimeError) as _error:
                pass
        result["notify_targets"] = [
            {"entity_id": s.entity_id, "name": s.name}
            for s in hass.states.async_all("notify")
        ]
        if action == "state":
            try:
                result["addons"] = [
                    {"slug": a["slug"], "name": a["name"]}
                    for a in await manager._handler.get_addons()
                ]
            except (HomeAssistantError, RuntimeError, NotImplementedError) as _error:
                result["addons"] = []
        connection.send_result(msg["id"], result)
    except (ValueError, HomeAssistantError, nas_backend.NasError) as error:
        # Backend NasError carries only sanitised messages. No raw SMB traces.
        connection.send_error(msg["id"], "operation_failed", str(error))


async def setup_panel(hass):
    if not hass.data.get("auto_backup_control_registered"):
        websocket_api.async_register_command(hass, command)
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    "/auto_backup_static",
                    str(Path(__file__).parent / "frontend"),
                    cache_headers=False,
                )
            ]
        )
        hass.data["auto_backup_control_registered"] = True
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path="auto-backup",
        webcomponent_name="auto-backup-panel",
        sidebar_title="Auto Backup",
        sidebar_icon="mdi:backup-restore",
        module_url="/auto_backup_static/panel.js?v=1.11.0-beta.3",
        require_admin=True,
        config_panel_domain=DOMAIN,
    )


@callback
def remove_panel(hass):
    frontend.async_remove_panel(hass, "auto-backup")
