"""One health table for the desk: services, VPN, emulators, Appium, staging URLs, Greybox, disk."""

from __future__ import annotations

import shutil
import socket
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from workday import devices, vpn
from workday.common import adb_devices, greybox_env, human_bytes, run
from workday.config import CONFIG, HOME


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warn: bool = False  # shown yellow; not counted as a failure


def _service(unit: str) -> Check:
    state = run(["systemctl", "is-active", unit]).stdout.strip()
    return Check(unit, state == "active", state)


def staging_urls() -> dict[str, str]:
    """Company -> base URL, from Greybox's BIZOM_WEB_URL / BIZOM_WEB_URL_<COMPANY>."""
    urls = {}
    for key, value in greybox_env().items():
        if key == "BIZOM_WEB_URL" or key.startswith("BIZOM_WEB_URL_"):
            name = key[len("BIZOM_WEB_URL_"):].lower() if key != "BIZOM_WEB_URL" else "default"
            if value.startswith("http"):
                urls[name] = value.rstrip("/")
    return urls


def _url(name: str, url: str) -> Check:
    context = ssl.create_default_context()
    request = urllib.request.Request(url + "/users/login", method="GET", headers={"User-Agent": "workday-health"})
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            return Check(f"web {name}", True, f"HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        return Check(f"web {name}", exc.code < 500, f"HTTP {exc.code}")
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return Check(f"web {name}", False, str(reason)[:60])


def _appium() -> Check:
    port = CONFIG["appium"]["port"]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=3) as response:
            return Check("appium", response.status == 200, f":{port} ready")
    except (urllib.error.URLError, OSError):
        return Check("appium", False, f"nothing on :{port}")


def collect(include_web: bool = True) -> list[Check]:
    checks = [_service(unit) for unit in CONFIG["services"]["units"]]
    checks.append(Check("vpn", vpn.is_connected(), vpn.status_line()))
    serials = adb_devices()
    names = devices.running_avds()
    want = CONFIG["emulators"]["avds"]
    missing = [avd for avd in want if avd not in names.values()]
    detail = ", ".join(f"{avd}={serial}" for serial, avd in names.items()) or "none running"
    if missing and names:
        detail += f" (missing {', '.join(missing)})"
    checks.append(Check("emulators", not missing, detail))
    others = [s for s in serials if s not in names]
    if others:
        checks.append(Check("adb other", True, ", ".join(others)))
    checks.append(_appium())
    greybox = run(["pgrep", "-f", "greybox-mobithon-2026/main.py"]).returncode == 0
    checks.append(Check("greybox", greybox, "running" if greybox else "not running", warn=not greybox))
    free = shutil.disk_usage(HOME).free
    checks.append(Check("disk free", free > 20 * 1024**3, human_bytes(free), warn=free <= 20 * 1024**3))
    if include_web:
        urls = staging_urls()
        with ThreadPoolExecutor(max_workers=8) as pool:
            checks.extend(pool.map(lambda item: _url(*item), urls.items()))
    return checks


def table(checks: list[Check]) -> str:
    green, red, yellow, reset = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
    lines = []
    for check in checks:
        if check.ok:
            mark = f"{green}✔{reset}"
        elif check.warn:
            mark = f"{yellow}!{reset}"
        else:
            mark = f"{red}✘{reset}"
        lines.append(f"  {mark} {check.name:<16} {check.detail}")
    return "\n".join(lines)


def failures(checks: list[Check]) -> list[Check]:
    return [check for check in checks if not check.ok and not check.warn]
