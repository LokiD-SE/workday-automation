"""New Bizom APK in ~/Downloads -> installed on every running emulator (adb install -r).

Triggered by workday-apk.path (any change in ~/Downloads) and by `day start` / `day apk`.
The first run only records the APKs already there. `install -r` keeps the app's data and
login; a signature mismatch or a downgrade is reported, never "fixed" by uninstalling.
"""

from __future__ import annotations

import re
import shlex
import time
from pathlib import Path

from workday.common import adb_devices, adb_path, detach, load_state, notify, run, save_state
from workday.config import CONFIG, expand

PARTIAL_SUFFIXES = (".crdownload", ".part", ".download", ".tmp")


def aapt() -> str | None:
    tools = sorted((expand(CONFIG["paths"]["android_sdk"]) / "build-tools").glob("*/aapt"))
    return str(tools[-1]) if tools else None


def badging(path: Path) -> dict:
    tool = aapt()
    if not tool:
        return {}
    out = run([tool, "dump", "badging", str(path)], timeout=60).stdout
    match = re.search(r"package: name='([^']+)' versionCode='(\d+)' versionName='([^']*)'", out)
    if not match:
        return {}
    return {"package": match.group(1), "version_code": int(match.group(2)), "version_name": match.group(3)}


def installed_version(serial: str, package: str) -> int | None:
    out = run([adb_path(), "-s", serial, "shell", "dumpsys", "package", package], timeout=30).stdout
    match = re.search(r"versionCode=(\d+)", out)
    return int(match.group(1)) if match else None


def candidates() -> list[Path]:
    folder = expand(CONFIG["paths"]["downloads"])
    return sorted(folder.glob(CONFIG["apk"]["pattern"]), key=lambda p: p.stat().st_mtime)


def wait_stable(path: Path, seconds: float = 3, timeout: float = 600) -> bool:
    """True once the file stops growing and no partial download sits next to it."""
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        partial = any(path.with_name(path.name + s).exists() for s in PARTIAL_SUFFIXES)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last and size > 0 and not partial:
            return True
        last = size
        time.sleep(seconds)
    return False


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{int(stat.st_mtime)}:{stat.st_size}"


def install(path: Path, info: dict, serials: list[str], downgrade: bool = False) -> list[tuple[str, bool, str]]:
    results = []
    for serial in serials:
        current = installed_version(serial, info["package"]) if info.get("package") else None
        if current is not None and current == info.get("version_code"):
            results.append((serial, True, f"already {info['version_name']}"))
            continue
        cmd = [adb_path(), "-s", serial, "install", "-r"] + (["-d"] if downgrade else []) + [str(path)]
        out = run(cmd, timeout=600)
        text = (out.stdout + out.stderr).strip()
        ok = out.returncode == 0 and "Success" in text
        if not ok:
            failure = re.search(r"(INSTALL_[A-Z_]+)", text)
            reason = failure.group(1) if failure else text.splitlines()[-1] if text else "failed"
            if reason == "INSTALL_FAILED_VERSION_DOWNGRADE":
                reason += " (use `day apk --downgrade`)"
            elif reason == "INSTALL_FAILED_UPDATE_INCOMPATIBLE":
                reason += " (signed differently - uninstall by hand if you accept losing app data)"
            results.append((serial, False, reason))
        else:
            results.append((serial, True, f"installed {info.get('version_name', path.name)}"))
    return results


def process(downgrade: bool = False, quiet: bool = False) -> list[str]:
    """Record new APKs and install the newest pending one. Returns printable lines."""
    state = load_state("apk")
    files = candidates()
    lines: list[str] = []
    if "seen" not in state:
        state = {"seen": {p.name: _fingerprint(p) for p in files}, "pending": "", "history": []}
        save_state("apk", state)
        return [f"baseline: {len(files)} existing APK(s) recorded, none installed"]
    for path in files:
        if state["seen"].get(path.name) == _fingerprint(path):
            continue
        if not wait_stable(path):
            continue
        state["seen"][path.name] = _fingerprint(path)
        state["pending"] = path.name
        lines.append(f"new APK: {path.name}")
    save_state("apk", state)
    pending = state.get("pending")
    if not pending:
        return lines or ["no new APK"]
    path = expand(CONFIG["paths"]["downloads"]) / pending
    if not path.exists():
        state["pending"] = ""
        save_state("apk", state)
        return lines + [f"{pending} disappeared"]
    info = badging(path) or {"version_name": path.name}
    serials = adb_devices()
    if not serials:
        lines.append(f"{info.get('version_name')} waiting - no device running (installs at `day start`)")
        if not quiet:
            notify("New Bizom APK", f"{path.name} ({info.get('version_name')}) will install when an emulator is up.")
        return lines
    results = install(path, info, serials, downgrade=downgrade)
    ok_all = all(ok for _, ok, _ in results)
    summary = "\n".join(f"{serial}: {msg}" for serial, _, msg in results)
    lines.extend(f"  {serial}: {msg}" for serial, _, msg in results)
    state["history"] = (state.get("history", []) + [{
        "at": time.strftime("%Y-%m-%d %H:%M:%S"), "apk": path.name, **info,
        "results": [{"serial": s, "ok": ok, "msg": m} for s, ok, m in results]}])[-50:]
    if ok_all:
        state["pending"] = ""
    save_state("apk", state)
    fresh = any(ok and msg.startswith("installed") for _, ok, msg in results)
    verdict = "install problem" if not ok_all else "installed" if fresh else "already installed"
    notify(f"Bizom {info.get('version_name', '')} {verdict}",
           summary, "normal" if ok_all else "critical")
    smoke = CONFIG["apk"].get("smoke_command")
    if ok_all and smoke:
        cmd = smoke.format(apk=shlex.quote(str(path)), version=info.get("version_name", ""))
        detach(["bash", "-lc", cmd], "apk-smoke")
        lines.append(f"smoke started: {cmd}")
    return lines
