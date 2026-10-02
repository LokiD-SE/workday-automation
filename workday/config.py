"""Settings: the defaults below, overridden by ~/.config/workday/config.toml (same keys)."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

HOME = Path.home()
CONFIG_FILE = HOME / ".config" / "workday" / "config.toml"
STATE_DIR = HOME / ".local" / "state" / "workday"
LOG_DIR = HOME / ".cache" / "workday"

DEFAULTS: dict = {
    "paths": {
        "greybox": str(HOME / "Workspace" / "greybox-mobithon-2026"),
        "testplanreport": str(HOME / "Downloads" / "TestPlanReport"),
        "downloads": str(HOME / "Downloads"),
        "workspace": str(HOME / "Workspace"),
        "android_sdk": str(HOME / "Android" / "Sdk"),
    },
    "services": {"units": ["mysql", "php7.4-fpm", "nginx"], "helper": "/usr/local/sbin/bizom-env"},
    "emulators": {"avds": ["Pixel_8", "Pixel_8_2"], "boot_timeout": 240},
    "appium": {"unit": "workday-appium.service", "port": 4723},
    "vpn": {
        "unit": "bizom-vpn.service",
        "socket": "/run/bizom-vpn/mgmt.sock",
        "connect_timeout": 180,
        "probe_host": "vpn-mumbai.mobisy.com",
    },
    "apk": {"pattern": "bizom*.apk", "smoke_command": ""},
    "watch": {
        # Greybox's PR checkouts (Repositories = bizom-laravel, with kmm and web2 inside).
        "repos": ["Repositories", "Repositories/bizomweb2", "Repositories/bizom-kmm"],
        "closed_statuses": ["closed", "closed and live", "cancelled", "rejected", "done", "completed"],
        # e.g. "qa-run generate {key}" - run once for every newly assigned task. Off by default.
        "on_new_task": "",
    },
    "report": {"dir": str(HOME / "Documents" / "DailyReports")},
    "recorder": {
        "idle_minutes": 15,
        "record_screen": True,
        "segment_seconds": 30,
        "keep_segments": 4,
        "pull_segments": 3,
        "bit_rate": 2000000,
    },
    "backup": {
        "remote": "gdrive:Bizom-QA-Backup",
        "stale_days": 14,
        "keep_newest_apks": 2,
        # name -> (local folder, remote sub-folder); each top-level entry is one unit.
        "folders": {
            "TestPlanReport": ["~/Downloads/TestPlanReport", "TestPlanReport"],
            "SS": ["~/Downloads/SS", "Downloads/SS"],
            "bugs": ["~/Downloads/bugs", "Downloads/bugs"],
        },
        "apk_glob": "~/Downloads/bizom*.apk",
        "apk_remote": "APKs",
        # Deleted without upload (heap / crash dumps).
        "crash_globs": ["~/*.hprof", "~/Downloads/*.hprof", "~/java_error_in_*.log", "~/hs_err_pid*.log"],
    },
    "git": {"test_commands": {"greybox-mobithon-2026": ".venv/bin/python -m pytest tests/ -q -x -p no:cacheprovider"}},
}


def _merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load(path: Path = CONFIG_FILE) -> dict:
    config = copy.deepcopy(DEFAULTS)
    if path.exists():
        with path.open("rb") as handle:
            _merge(config, tomllib.load(handle))
    return config


def expand(path: str) -> Path:
    return Path(path).expanduser()


CONFIG = load()
