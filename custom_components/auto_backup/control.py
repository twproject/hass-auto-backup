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
        return validate_policy(self.entry.options.get("policy", {}))

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
        due = due_slots(self.policy, now, self.claims)
        if due:
            kind, key, _ = due[0]
            self.claims[key] = now.isoformat()
            # Durable claim before spawning: restart cannot duplicate a claimed slot.
            await self.save()
            self.launch(kind, "schedule", key)
            return
        health = self.manager.nas_status.get("status")
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
        last = self.manager.nas_status.get("last_verified")
        stamp = dt_util.parse_datetime(last) if last else None
        if self.policy["notify_errors"] and (
            stamp is None
            or (now - stamp).total_seconds() > self.policy["stale_hours"] * 3600
        ):
            await self.alert_once(
                "missing:" + now.date().isoformat(),
                "No recent verified NAS backup. Check Auto Backup.",
            )

    async def alert_once(self, key, message):
        if key in self.alerts:
            return
        self.alerts[key] = dt_util.now().isoformat()
        self.alerts = dict(list(self.alerts.items())[-100:])
        await self.save()
        await self.notify(message)

    def launch(self, kind, actor, slot=None):
        if self.stopping or self.busy or (self.task and not self.task.done()):
            raise HomeAssistantError("Another backup operation is already running")
        if kind not in ("backup", "verify", "check", "report"):
            raise HomeAssistantError("Unknown operation")
        if not self.manager._nas.enabled:
            raise HomeAssistantError(
                "Configure and enable a verified NAS destination first"
            )
        self.task = self.hass.async_create_task(self.run(kind, actor, slot))

    async def run(self, kind, actor, slot=None):
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
                    policy = self.policy
                    result = await self.manager.async_create_backup(
                        {
                            "name": policy["name"]
                            + dt_util.now().strftime(" %Y-%m-%d %H:%M"),
                            "exclude": {
                                "folders": policy["exclude_folders"],
                                "addons": policy["exclude_addons"],
                            },
                            "encrypted": policy["encrypted"],
                            "exclude_database": policy["exclude_database"],
                        }
                    )
                    if not result:
                        raise HomeAssistantError(
                            "Backup creation or NAS verification failed"
                        )
                elif kind == "verify":
                    await self.manager._nas.verify(False)
                    await self.manager._nas.check()
                elif kind == "check":
                    await self.manager._nas.check()
                else:
                    await self.manager._nas.check()
                    status = self.manager.nas_status
                    await self.notify(
                        f"Backup summary\nStatus: {status.get('status')}\nLast backup: {status.get('last_backup', 'none')}\nCopies: {status.get('copies', 0)}\nDaily / Sunday / monthly: {status.get('daily', 0)} / {status.get('weekly', 0)} / {status.get('monthly', 0)}\nMissing checksums: {status.get('missing_checksums', 0)}"
                    )
                item["status"] = "success"
                if kind == "backup" and self.policy["notify_success"]:
                    await self.notify(
                        "Home Assistant backup copied to NAS and verified with SHA-256."
                    )
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
            "next": next_slots(self.policy, dt_util.now(), self.claims),
            "busy": self.busy or bool(self.task and not self.task.done()),
            "nas": self.manager.nas_status,
            "history": list(reversed(self.history)),
            "copies": copies,
        }
