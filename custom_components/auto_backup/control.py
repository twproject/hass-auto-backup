"""Persistent controller: schedules, operations, notifications and audit log."""

import asyncio
from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .control_policy import validate_policy, due_slots, next_slots
from .nas_manager import EVENT_NAS_UPDATED
from .control_request import (
    policy_request,
    manager_payload,
    validate_request,
    check_capabilities,
    check_directories,
)
from . import nas_backend


class BackupControl:
    def __init__(self, hass, entry, manager):
        self.hass, self.entry, self.manager = hass, entry, manager
        self.store = Store(hass, 1, "auto_backup.control")
        self.history, self.claims, self.alerts = [], {}, {}
        self.task = None
        self.operation_lock = asyncio.Lock()
        self.cancels = []
        self.stopping = False

    @property
    def policy(self):
        value = dict(self.entry.options.get("policy", {}))
        if not getattr(self.manager, "_supervised", True):
            value.setdefault("exclude_folders", [])
        return validate_policy(value)

    @property
    def busy(self):
        return (
            self.operation_lock.locked()
            or self.manager._backup_lock.locked()
            or self.manager._nas.lock.locked()
        )

    @property
    def revision(self):
        return hashlib.sha256(
            json.dumps(dict(self.entry.options), sort_keys=True).encode()
        ).hexdigest()

    async def load(self):
        saved = await self.store.async_load() or {}
        self.history = saved.get("history", [])[-100:]
        self.claims = saved.get("claims", {})
        self.alerts = saved.get("alerts", {})
        for item in self.history:
            if item["status"] == "running":
                item.update(
                    status="interrupted",
                    ended_at=dt_util.now().isoformat(),
                    error="Home Assistant restarted during the operation",
                )
        await self.save()
        self.cancels.append(
            async_track_time_interval(self.hass, self.tick, timedelta(minutes=1))
        )
        self.cancels.append(self.hass.bus.async_listen(EVENT_NAS_UPDATED, self.changed))
        self.hass.async_create_task(self.tick(None))

    async def save(self):
        cutoff = (dt_util.now() - timedelta(days=35)).date().isoformat()
        self.claims = {
            k: v for k, v in self.claims.items() if k.split(":", 1)[-1] >= cutoff
        }
        await self.store.async_save(
            {
                "history": self.history[-100:],
                "claims": self.claims,
                "alerts": self.alerts,
            }
        )

    def changed(self, _=None):
        self.hass.bus.async_fire("auto_backup.control_updated")

    async def stop(self):
        self.stopping = True
        for cancel in self.cancels:
            cancel()
        self.cancels.clear()
        # Executor transfers cannot safely be cancelled midway. Let them commit.
        if self.task and not self.task.done():
            await asyncio.shield(self.task)

    async def notify(self, message):
        for entity in self.policy["notify_entities"]:
            try:
                await self.hass.services.async_call(
                    "notify",
                    "send_message",
                    {"entity_id": entity, "message": message},
                    blocking=True,
                )
            except Exception:
                # The failure remains visible even when the notification endpoint fails.
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Auto Backup",
                        "message": "Notification delivery failed. Check the Auto Backup operation log.",
                        "notification_id": "auto_backup_notification",
                    },
                    blocking=True,
                )
        if not self.policy["notify_entities"]:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Auto Backup",
                    "message": message,
                    "notification_id": "auto_backup_status",
                },
                blocking=True,
            )

    def effective_schedule(self):
        policy = dict(self.policy)
        if not self.manager._nas.enabled:
            policy["verify_enabled"] = False
        return policy

    async def purge_preview(self):
        if self.manager._nas.enabled:
            result = await self.hass.async_add_executor_job(
                nas_backend.scan,
                dict(self.manager._nas.config),
                self.manager._nas.namespace,
                dt_util.now(),
            )
            remote = result.get("plan", {}).get("delete", [])
            receipts = sorted(
                (
                    r
                    for r in self.manager._nas.receipts.values()
                    if r.get("slug") and not r.get("local_removed")
                ),
                key=lambda r: r.get("created_at", ""),
                reverse=True,
            )
            local = [
                r["slug"] for r in receipts[self.manager._nas.config["keep_local"] :]
            ]
        else:
            remote, local = [], sorted(self.manager.get_purgeable_snapshots())
        result = {"nas": remote, "local": local}
        result["confirmation"] = hashlib.sha256(
            (self.revision + json.dumps(result, sort_keys=True)).encode()
        ).hexdigest()
        return result

    async def tick(self, _):
        if self.stopping or not self.policy["enabled"]:
            return
        now = dt_util.now()
        if self.busy:
            running = next(
                (i for i in reversed(self.history) if i["status"] == "running"), None
            )
            if (
                running
                and self.policy["notify_errors"]
                and (
                    now - dt_util.parse_datetime(running["started_at"])
                ).total_seconds()
                > self.policy["stalled_minutes"] * 60
            ):
                await self.alert_once(
                    "stalled:" + running["id"],
                    "Backup operation has exceeded the configured duration. It has not been cancelled.",
                )
            return
        due = due_slots(self.effective_schedule(), now, self.claims)
        if due:
            kind, key, _ = due[0]
            self.claims[key] = now.isoformat()
            # Durable claim before spawning: restart cannot duplicate a claimed slot.
            await self.save()
            self.launch(kind, "schedule", key)
            return
        health = (
            self.manager.nas_status.get("status") if self.manager._nas.enabled else "ok"
        )
        problems = {
            "error",
            "unknown",
            "unavailable",
            "disabled",
            "missing_checksums",
            "empty",
        }
        if self.policy["notify_errors"] and health in problems:
            since = self.alerts.get("nas_problem_since")
            if since is None:
                self.alerts["nas_problem_since"] = now.isoformat()
                await self.save()
            else:
                elapsed = (now - dt_util.parse_datetime(since)).total_seconds()
                if elapsed >= (300 if health == "empty" else 900):
                    await self.alert_once(
                        "nas_problem:" + now.date().isoformat(),
                        "NAS backup monitoring reports a persistent problem ("
                        + health
                        + "). Check Auto Backup.",
                    )
        elif self.alerts.pop("nas_problem_since", None) is not None:
            await self.save()
        last = (
            self.manager.nas_status.get("last_verified")
            if self.manager._nas.enabled
            else next(
                (
                    h.get("ended_at")
                    for h in reversed(self.history)
                    if h["kind"] == "backup" and h["status"] == "success"
                ),
                None,
            )
        )
        stamp = dt_util.parse_datetime(last) if last else None
        if self.policy["notify_errors"] and (
            stamp is None
            or (now - stamp).total_seconds() > self.policy["stale_hours"] * 3600
        ):
            await self.alert_once(
                "missing:" + now.date().isoformat(),
                "No recent successful backup. Check Auto Backup.",
            )

    async def alert_once(self, key, message):
        if key in self.alerts:
            return
        self.alerts[key] = dt_util.now().isoformat()
        self.alerts = dict(list(self.alerts.items())[-100:])
        await self.save()
        await self.notify(message)

    def launch(self, kind, actor, slot=None, request=None, confirmation=None):
        if self.stopping or self.busy or (self.task and not self.task.done()):
            raise HomeAssistantError("Another backup operation is already running")
        if kind not in ("backup", "verify", "check", "report", "purge", "adopt"):
            raise HomeAssistantError("Unknown operation")
        if kind in ("verify", "check", "adopt") and not self.manager._nas.enabled:
            raise HomeAssistantError(
                "Configure and enable a verified NAS destination first"
            )
        if kind == "adopt" and (
            self.policy["enabled"] or not self.manager._nas.config.get("manage_legacy")
        ):
            raise ValueError("Disable the schedule before legacy adoption")
        if kind in ("purge", "adopt") and not confirmation:
            raise ValueError("Review and confirm the operation first")
        if kind == "backup":
            request = (
                validate_request(request)
                if request
                else policy_request(self.policy, self.entry.options, dt_util.now())
            )
            check_capabilities(
                request,
                self.manager._nas.enabled,
                getattr(self.manager, "_supervised", True),
            )
        self.task = self.hass.async_create_task(
            self.run(kind, actor, slot, request=request, confirmation=confirmation)
        )

    async def run(self, kind, actor, slot=None, request=None, confirmation=None):
        async with self.operation_lock:
            item = {
                "id": uuid4().hex,
                "kind": kind,
                "actor": actor,
                "slot": slot,
                "status": "running",
                "started_at": dt_util.now().isoformat(),
            }
            self.history.append(item)
            self.history = self.history[-100:]
            await self.save()
            self.changed()
            try:
                if kind == "backup":
                    request = request or policy_request(
                        self.policy, self.entry.options, dt_util.now()
                    )
                    check_capabilities(
                        request,
                        self.manager._nas.enabled,
                        getattr(self.manager, "_supervised", True),
                    )
                    await self.hass.async_add_executor_job(check_directories, request)
                    item["service"] = request["service"]
                    result = await self.manager.async_create_backup(
                        manager_payload(request)
                    )
                    if not result:
                        raise HomeAssistantError(
                            "Backup creation or NAS verification failed"
                        )
                elif kind in ("verify", "adopt"):
                    if kind == "adopt" and confirmation != self.revision:
                        raise ValueError("Settings changed; review the operation again")
                    await self.manager._nas.verify(kind == "adopt")
                    await self.manager._nas.check()
                elif kind == "check":
                    await self.manager._nas.check()
                elif kind == "purge":
                    async with self.manager._backup_lock:
                        preview = await self.purge_preview()
                        if confirmation != preview["confirmation"]:
                            raise ValueError(
                                "Backup inventory changed; review the operation again"
                            )
                        await self.manager.purge_backups()
                elif self.manager._nas.enabled:
                    await self.manager._nas.check()
                    status = self.manager.nas_status
                    await self.notify(
                        f"Backup summary\nStatus: {status.get('status')}\nLast backup: {status.get('last_backup', 'none')}\nCopies: {status.get('copies', 0)}\nDaily / Sunday / monthly: {status.get('daily', 0)} / {status.get('weekly', 0)} / {status.get('monthly', 0)}\nMissing checksums: {status.get('missing_checksums', 0)}"
                    )
                else:
                    backups = await self.manager._handler.get_backups()
                    await self.notify(
                        f"Backup summary\nLocal backups: {len(backups)}\nMonitored: {self.manager.monitored}\nExpired: {self.manager.purgeable}"
                    )
                item["status"] = "success"
                if kind == "backup" and self.policy["notify_success"]:
                    await self.notify("Home Assistant backup completed.")
            except Exception as error:
                item.update(status="error", error=type(error).__name__)
                if self.policy["notify_errors"]:
                    await self.notify(
                        "Auto Backup: "
                        + kind
                        + " failed ("
                        + type(error).__name__
                        + "). Previous copies have been preserved; check the operation log."
                    )
            finally:
                item["ended_at"] = dt_util.now().isoformat()
                await self.save()
                self.changed()

    def state(self):
        options = dict(self.entry.options)
        options.pop("nas_password", None)
        options.pop("backup_password", None)
        options["backup_password_set"] = bool(self.entry.options.get("backup_password"))
        options["nas_password_set"] = bool(self.entry.options.get("nas_password"))
        catalog = getattr(self.manager._nas, "catalog", None)
        inventory = {row["name"]: row for row in catalog or []}
        copies = []
        for name in set(inventory) | {
            n for n, r in self.manager._nas.receipts.items() if not r.get("nas_removed")
        }:
            receipt = self.manager._nas.receipts.get(name, {})
            row = {
                k: receipt.get(k)
                for k in (
                    "slug",
                    "created_at",
                    "bytes",
                    "verified_at",
                    "verification",
                    "sha256",
                    "local_removed",
                )
            }
            row.update(inventory.get(name, {}))
            row.update(
                name=name, present=None if catalog is None else name in inventory
            )
            copies.append(row)
        return {
            "revision": self.revision,
            "options": options,
            "policy": self.policy,
            "timezone": self.hass.config.time_zone,
            "next": next_slots(self.effective_schedule(), dt_util.now(), self.claims),
            "capabilities": {
                "supervised": getattr(self.manager, "_supervised", True),
                "nas_enabled": self.manager._nas.enabled,
            },
            "local_success": next(
                (
                    h
                    for h in reversed(self.history)
                    if h["kind"] == "backup" and h["status"] == "success"
                ),
                None,
            ),
            "busy": self.busy or bool(self.task and not self.task.done()),
            "nas": self.manager.nas_status,
            "history": list(reversed(self.history)),
            "copies": copies,
        }
