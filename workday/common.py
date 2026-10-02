"""Small shared helpers: notifications, JSON state, subprocess, Greybox .env, adb."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from workday.config import CONFIG, LOG_DIR, STATE_DIR, expand


def notify(title: str, body: str = "", urgency: str = "normal") -> None:
    """Desktop notification; silent when there is no session bus (tests, ssh)."""
    if os.environ.get("WORKDAY_NO_NOTIFY") or not shutil.which("notify-send"):
        return
    subprocess.run(["notify-send", "-a", "Workday", "-u", urgency, title, body[:900]],
                   capture_output=True, timeout=10, check=False)


def load_state(name: str, default=None):
    path = STATE_DIR / f"{name}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def save_state(name: str, data) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{name}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, default=str))
    tmp.replace(path)
    return path


def run(cmd: list[str], timeout: float = 60, **kw) -> subprocess.CompletedProcess:
    """Run and capture; never raises on a non-zero exit or a missing binary."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, **kw)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, exc.stdout or "", f"timed out after {timeout}s")


def detach(cmd: list[str], log_name: str, cwd: str | None = None) -> subprocess.Popen:
    """Start a process that outlives this one, output to ~/.cache/workday/<log_name>.log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_DIR / f"{log_name}.log", "ab")
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            cwd=cwd, start_new_session=True)


def greybox_env() -> dict[str, str]:
    """KEY=VALUE pairs of Greybox's .env (read-only, values never logged)."""
    values: dict[str, str] = {}
    path = expand(CONFIG["paths"]["greybox"]) / ".env"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def adb_path() -> str:
    sdk = expand(CONFIG["paths"]["android_sdk"]) / "platform-tools" / "adb"
    return str(sdk) if sdk.exists() else (shutil.which("adb") or "adb")


def adb_devices() -> list[str]:
    """Serials in the `device` state."""
    out = run([adb_path(), "devices"], timeout=20).stdout
    return [line.split()[0] for line in out.splitlines()[1:] if line.strip().endswith("\tdevice")]


def ask(question: str, default: bool = False) -> bool:
    if not sys.stdin.isatty():
        return default
    hint = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {hint} ").strip().lower()
    return default if not answer else answer in {"y", "yes"}


def human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"
