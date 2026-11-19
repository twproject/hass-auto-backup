"""Validated, UI-owned policy and local calendar scheduling (no YAML generation)."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re

DEFAULT_POLICY = {
    "enabled": False,
    "backup_time": "09:53",
    "backup_days": list(range(7)),
    "name": "Home Assistant",
    "exclude_folders": ["media"],
    "exclude_addons": [],
    "exclude_database": False,
    "encrypted": False,
    "verify_enabled": True,
    "verify_day": 6,
    "verify_time": "03:00",
    "report_enabled": True,
    "report_day": 6,
    "report_time": "12:30",
    "notify_entities": [],
    "notify_errors": True,
    "notify_success": False,
    "stale_hours": 26,
    "stalled_minutes": 90,
    "catchup_hours": 12,
}


def validate_policy(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ValueError("Unsupported policy field")
    policy = {**deepcopy(DEFAULT_POLICY), **value}
    for key in (
        "enabled",
        "exclude_database",
        "encrypted",
        "verify_enabled",
        "report_enabled",
        "notify_errors",
        "notify_success",
    ):
        if type(policy[key]) is not bool:
            raise ValueError("Invalid switch: " + key)
    for key in ("backup_time", "verify_time", "report_time"):
        if not isinstance(policy[key], str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", policy[key]
        ):
            raise ValueError("Invalid time: " + key)
    for key, low, high in (
        ("verify_day", 0, 6),
        ("report_day", 0, 6),
        ("stale_hours", 1, 720),
        ("stalled_minutes", 5, 1440),
        ("catchup_hours", 0, 23),
    ):
        if type(policy[key]) is not int or not low <= policy[key] <= high:
            raise ValueError("Invalid limit: " + key)
    days = policy["backup_days"]
    if (
        not isinstance(days, list)
        or not days
        or len(days) > 7
        or any(type(d) is not int or not 0 <= d <= 6 for d in days)
    ):
        raise ValueError("Select at least one backup day")
    policy["backup_days"] = sorted(set(days))
    if (
        not isinstance(policy["name"], str)
        or not 1 <= len(policy["name"].strip()) <= 80
        or any(c in policy["name"] for c in "\r\n")
    ):
        raise ValueError("Backup name must contain 1–80 characters")
    policy["name"] = policy["name"].strip()
    for key in ("exclude_folders", "exclude_addons", "notify_entities"):
        values = policy[key]
        if (
            not isinstance(values, list)
            or len(values) > 200
            or any(not isinstance(v, str) for v in values)
        ):
            raise ValueError("Invalid selection: " + key)
        if key == "exclude_folders" and set(values) - {
            "homeassistant",
            "ssl",
            "share",
            "media",
            "addons/local",
        }:
            raise ValueError("Unknown backup folder")
        if key == "exclude_folders" and "homeassistant" in values:
            raise ValueError("Home Assistant configuration must be included")
        if key == "exclude_addons" and any(
            not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", v) for v in values
        ):
            raise ValueError("Invalid app identifier")
        if key == "notify_entities" and any(
            not re.fullmatch(r"notify\.[a-z0-9_]+", v) for v in values
        ):
            raise ValueError("Select notify entities")
        policy[key] = sorted(set(values))
    return policy


def occurrences(policy, now, days=9):
    """One slot per local calendar date, including after a DST fold."""
    result = []
    if not policy["enabled"]:
        return result
    for offset in range(days):
        day = now.date() + timedelta(days=offset)
        for kind, allowed, clock in (
            ("backup", day.weekday() in policy["backup_days"], policy["backup_time"]),
            (
                "verify",
                policy["verify_enabled"] and day.weekday() == policy["verify_day"],
                policy["verify_time"],
            ),
            (
                "report",
                policy["report_enabled"] and day.weekday() == policy["report_day"],
                policy["report_time"],
            ),
        ):
            if allowed:
                hour, minute = map(int, clock.split(":"))
                stamp = datetime(
                    day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo
                )
                # Spring-forward gaps run at the corresponding first valid time.
                stamp = stamp.astimezone(timezone.utc).astimezone(now.tzinfo)
                result.append((kind, f"{kind}:{day.isoformat()}", stamp))
    return sorted(result, key=lambda item: item[2].astimezone(timezone.utc))


def due_slots(policy, now, claims):
    now_utc = now.astimezone(timezone.utc)
    return [
        item
        for item in occurrences(policy, now, 1)
        if item[1] not in claims
        and 0
        <= (now_utc - item[2].astimezone(timezone.utc)).total_seconds()
        <= policy["catchup_hours"] * 3600 + 60
    ]


def next_slots(policy, now, claims):
    result = {}
    for kind, key, stamp in occurrences(policy, now):
        if (
            kind not in result
            and key not in claims
            and stamp.astimezone(timezone.utc) > now.astimezone(timezone.utc)
        ):
            result[kind] = stamp.isoformat()
    return result
