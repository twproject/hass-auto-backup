"""Admin-only complete Auto Backup interface, no application-specific settings."""

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
from .control_request import (
    policy_request,
    validate_request,
    check_capabilities,
    check_directories,
)
from .nas_manager import config_from_options
from .nas_policy import LEGACY
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
    "backup_password",
    "clear_backup_password",
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
    for secret in ("nas_password", "backup_password"):
        if secret in changes and not changes[secret]:
            result[secret] = current.get(secret, "")
    clear = result.pop("clear_backup_password", False)
    if type(clear) is not bool:
        raise ValueError("Invalid password setting")
    if clear:
        result.pop("backup_password", None)
    for key in ("auto_purge", "nas_enabled", "nas_manage_legacy"):
        if type(result.get(key, False)) is not bool:
            raise ValueError("Invalid switch: " + key)
    if (
        type(result["backup_timeout"]) is not int
        or not 1 <= result["backup_timeout"] <= 1440
    ):
        raise ValueError("Backup timeout must be between 1 and 1440 minutes")
    for key in (
        "nas_server",
        "nas_share",
        "nas_folder",
        "nas_username",
        "nas_workgroup",
        "nas_password",
        "backup_password",
    ):
        if not isinstance(result.get(key, ""), str) or len(result.get(key, "")) > 1024:
            raise ValueError("Invalid settings text")
    result["policy"] = validate_policy(result.get("policy", {}))
    if result.get("nas_enabled"):
        nas_backend.validate_config(config_from_options(result))
    return result


async def prepare_request(hass, manager, request):
    request = validate_request(request)
    check_capabilities(request, manager._nas.enabled, is_hassio(hass))
    await hass.async_add_executor_job(check_directories, request)
    if (
        request["data"].get("encrypted")
        and not request["data"].get("password")
        and not manager._manager.config.data.create_backup.password
    ):
        raise ValueError("Configure the recovery key before enabling encryption")
    return request


@websocket_api.websocket_command(
    {
        vol.Required("type"): "auto_backup/control",
        vol.Required("action"): vol.In(
            [
                "state",
                "save",
                "test_connection",
                "backup",
                "verify",
                "check",
                "report",
                "purge_preview",
                "purge",
                "adopt_preview",
                "adopt",
            ]
        ),
        vol.Optional("options"): dict,
        vol.Optional("revision"): str,
        vol.Optional("request"): dict,
        vol.Optional("draft"): dict,
        vol.Optional("password"): str,
        vol.Optional("confirmation"): str,
        vol.Optional("use_saved_password", default=False): bool,
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
        if (
            action in ("save", "test_connection", "purge_preview", "adopt_preview")
            and control.busy
        ):
            raise ValueError("Wait for the running operation before changing settings")
        if action in ("save", "test_connection"):
            current = dict(control.entry.options)
            current.setdefault("policy", control.policy)
            options = validate_options(current, msg.get("options", {}))
            if options.get("nas_enabled") and not is_hassio(hass):
                raise ValueError("NAS mode requires Supervisor")
            if action == "test_connection":
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
            request = policy_request(options["policy"], options, dt_util.now())
            check_capabilities(
                request, options.get("nas_enabled", False), is_hassio(hass)
            )
            await hass.async_add_executor_job(check_directories, request)
            if (
                request["data"].get("encrypted")
                and not manager._manager.config.data.create_backup.password
            ):
                raise ValueError(
                    "Configure the recovery key before enabling encryption"
                )
            hass.config_entries.async_update_entry(control.entry, options=options)
            await manager.update_listener(hass, control.entry)
            control.changed()
        elif action == "purge_preview":
            connection.send_result(msg["id"], await control.purge_preview())
            return
        elif action == "adopt_preview":
            if not manager._nas.enabled or not manager._nas.config.get("manage_legacy"):
                raise ValueError("Enable legacy adoption in NAS settings first")
            result = await manager._nas.check()
            connection.send_result(
                msg["id"],
                {
                    "legacy": [
                        r["name"]
                        for r in manager._nas.catalog or []
                        if LEGACY.fullmatch(r["name"])
                    ],
                    "confirmation": control.revision,
                },
            )
            return
        elif action != "state":
            request = msg.get("request")
            if request is not None and action != "backup":
                raise ValueError("Backup request is only valid for backup actions")
            if action == "backup":
                if "draft" in msg:
                    secret = (
                        control.entry.options.get("backup_password", "")
                        if msg.get("use_saved_password")
                        else msg.get("password", "")
                    )
                    request = policy_request(
                        validate_policy(msg["draft"]),
                        {"backup_password": secret},
                        dt_util.now(),
                    )
                if request is None:
                    request = policy_request(
                        control.policy, control.entry.options, dt_util.now()
                    )
                elif msg.get("use_saved_password"):
                    request = {
                        **request,
                        "data": {
                            **request.get("data", {}),
                            "password": control.entry.options.get(
                                "backup_password", ""
                            ),
                        },
                    }
                    if not request["data"]["password"]:
                        raise ValueError("A custom backup password is required")
                request = await prepare_request(hass, manager, request)
            control.launch(
                action,
                connection.user.id,
                request=request,
                confirmation=msg.get("confirmation"),
            )
        result = control.state()
        result["notify_targets"] = [
            {"entity_id": s.entity_id, "name": s.name}
            for s in hass.states.async_all("notify")
        ]
        if action == "state":
            result["local_backups"] = []
            try:
                local = await manager._handler.get_backups()
                slugs = {item["slug"] for item in local}
                for item in result["copies"]:
                    item["local_present"] = item.get("slug") in slugs
                for item in local:
                    slug = item["slug"]
                    result["local_backups"].append(
                        {
                            "slug": slug,
                            "name": item.get("name", slug),
                            "date": item.get("date"),
                            "size": item.get("size"),
                            "protected": item.get("protected"),
                            "expires": manager._snapshots.get(slug),
                            "expired": slug in manager.get_purgeable_snapshots(),
                        }
                    )
            except (HomeAssistantError, RuntimeError, NotImplementedError) as _error:
                result["inventory_error"] = True
            result["addons"], result["locations"] = [], []
            if is_hassio(hass):
                try:
                    result["addons"] = [
                        {"slug": a["slug"], "name": a["name"]}
                        for a in await manager._handler.get_addons()
                    ]
                    mounts = await manager._handler.send_command(
                        "/mounts", method="get"
                    )
                    result["locations"] = [
                        m["name"]
                        for m in mounts.get("data", {}).get("mounts", [])
                        if m.get("usage") == "backup"
                    ]
                except (
                    HomeAssistantError,
                    RuntimeError,
                    NotImplementedError,
                ) as _error:
                    pass
        connection.send_result(msg["id"], result)
    except (ValueError, HomeAssistantError, nas_backend.NasError) as error:
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
        module_url="/auto_backup_static/panel.js?v=1.12.0-beta.5",
        require_admin=True,
        config_panel_domain=DOMAIN,
    )


@callback
def remove_panel(hass):
    frontend.async_remove_panel(hass, "auto-backup")
