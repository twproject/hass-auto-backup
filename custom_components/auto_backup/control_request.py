"""Complete, secret-safe mapping of GUI requests to Auto Backup services."""

from copy import deepcopy
from datetime import datetime
import math
import os

COMMON = {
    "name",
    "password",
    "keep_days",
    "download_path",
    "compressed",
    "location",
    "encrypted",
    "exclude_database",
}
SERVICE_FIELDS = {
    "backup": COMMON
    | {
        "include",
        "exclude",
        "include_addons",
        "include_folders",
        "exclude_addons",
        "exclude_folders",
    },
    "backup_full": COMMON | {"exclude"},
    "backup_partial": COMMON | {"addons", "folders"},
}
FOLDERS = {
    "homeassistant",
    "config",
    "home assistant configuration",
    "ssl",
    "share",
    "media",
    "addons",
    "addons/local",
    "local add-ons",
}


def selection(value, folders=False, paths=False):
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError("Invalid backup selection")
    if any(
        not isinstance(v, str)
        or not v.strip()
        or len(v) > 200
        or any(c in v for c in "\x00\r\n")
        for v in value
    ):
        raise ValueError("Invalid backup selection")
    if folders and any(v.casefold() not in FOLDERS for v in value):
        raise ValueError("Unknown backup folder")
    if (
        not folders
        and not paths
        and any(v.startswith(("../", "./", "/")) or v == ".." for v in value)
    ):
        raise ValueError("Invalid app identifier")
    if folders:
        aliases = {
            "config": "homeassistant",
            "home assistant configuration": "homeassistant",
            "addons": "addons/local",
            "local add-ons": "addons/local",
        }
        value = [aliases.get(v.casefold(), v.casefold()) for v in value]
    return list(dict.fromkeys(value))


def validate_request(request):
    if (
        not isinstance(request, dict)
        or set(request) != {"service", "data"}
        or not isinstance(request["service"], str)
        or request["service"] not in SERVICE_FIELDS
    ):
        raise ValueError("Unknown backup service")
    service, data = request["service"], deepcopy(request["data"])
    if not isinstance(data, dict) or set(data) - SERVICE_FIELDS[service]:
        raise ValueError("Unknown backup field")
    for field in ("compressed", "encrypted", "exclude_database"):
        if field in data and type(data[field]) is not bool:
            raise ValueError("Invalid backup switch")
    for field, limit in (("name", 200), ("password", 1024), ("location", 200)):
        if (
            field in data
            and data[field] is not None
            and (
                not isinstance(data[field], str)
                or len(data[field]) > limit
                or any(c in data[field] for c in "\x00\r\n")
            )
        ):
            raise ValueError("Invalid backup text field")
    if "keep_days" in data:
        days = data["keep_days"]
        if (
            type(days) not in (int, float)
            or not math.isfinite(days)
            or not 0 <= days <= 10000
        ):
            raise ValueError("Invalid backup expiry")
    if "download_path" in data:
        data["download_path"] = selection(data["download_path"], paths=True)
        if any(not os.path.isabs(p) for p in data["download_path"]):
            raise ValueError("Download directories must be absolute existing paths")
    for field in ("include", "exclude"):
        if field in data:
            block = data[field]
            if not isinstance(block, dict) or set(block) - {"addons", "folders"}:
                raise ValueError("Invalid backup selection")
            data[field] = {
                "addons": selection(block.get("addons", [])),
                "folders": selection(block.get("folders", []), True),
            }
    for field in (
        "addons",
        "folders",
        "include_addons",
        "include_folders",
        "exclude_addons",
        "exclude_folders",
    ):
        if field in data:
            data[field] = selection(data[field], "folder" in field)
    if service == "backup" and any(
        k in data for k in ("include_addons", "include_folders")
    ):
        if "include" in data:
            raise ValueError("Use one include format")
        data["include"] = {
            "addons": data.pop("include_addons", []),
            "folders": data.pop("include_folders", []),
        }
    if service == "backup" and any(
        k in data for k in ("exclude_addons", "exclude_folders")
    ):
        if "exclude" in data:
            raise ValueError("Use one exclude format")
        data["exclude"] = {
            "addons": data.pop("exclude_addons", []),
            "folders": data.pop("exclude_folders", []),
        }
    if data.get("location") in ("", "/backup"):
        data.pop("location", None)
    if data.get("password") is None:
        data.pop("password", None)
    return {"service": service, "data": data}


def policy_request(policy, options, now):
    service = policy["backup_service"]
    data = {
        "name": policy["name"],
        "compressed": policy["compressed"],
        "exclude_database": policy["exclude_database"],
        "encrypted": policy["encryption"] == "default",
    }
    if data["name"] and policy["append_timestamp"]:
        data["name"] += now.strftime(" %Y-%m-%d %H:%M")
    if policy["encryption"] == "password":
        data["password"] = options.get("backup_password", "")
        if not data["password"]:
            raise ValueError("A custom backup password is required")
    if policy["keep_days"] is not None:
        data["keep_days"] = policy["keep_days"]
    if policy["location"]:
        data["location"] = policy["location"]
    if policy["download_path"]:
        data["download_path"] = policy["download_path"]
    if service == "backup_partial":
        data.update(addons=policy["include_addons"], folders=policy["include_folders"])
    else:
        if service == "backup" and policy["include_enabled"]:
            data["include"] = {
                "addons": policy["include_addons"],
                "folders": policy["include_folders"],
            }
        if policy["exclude_addons"] or policy["exclude_folders"]:
            data["exclude"] = {
                "addons": policy["exclude_addons"],
                "folders": policy["exclude_folders"],
            }
    return validate_request({"service": service, "data": data})


def check_capabilities(request, nas_enabled, supervised):
    data = request["data"]
    if nas_enabled and (data.get("download_path") or "keep_days" in data):
        raise ValueError(
            "Verified NAS uses calendar retention; disable NAS for keep_days or download_path"
        )
    if not supervised:
        if "exclude" in data or data.get("include") not in (
            None,
            {"addons": [], "folders": ["homeassistant"]},
        ):
            raise ValueError("Partial backups require Supervisor")
        if data.get("location") or data.get("compressed") is False:
            raise ValueError("This backup option requires Supervisor")


def check_directories(request):
    if any(
        not os.path.isdir(path) for path in request["data"].get("download_path", [])
    ):
        raise ValueError("Download directories must be absolute existing paths")


def manager_payload(request):
    data = deepcopy(request["data"])
    if request["service"] == "backup_partial":
        data["include"] = {
            "addons": data.pop("addons", []),
            "folders": data.pop("folders", []),
        }
    return data
