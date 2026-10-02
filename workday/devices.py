"""Android emulators and Appium."""

from __future__ import annotations

import time
import urllib.request

from workday.common import adb_devices, adb_path, detach, run
from workday.config import CONFIG, expand


def emulator_bin() -> str:
    return str(expand(CONFIG["paths"]["android_sdk"]) / "emulator" / "emulator")


def running_avds() -> dict[str, str]:
    """emulator serial -> AVD name, for running emulators."""
    names = {}
    for serial in adb_devices():
        if serial.startswith("emulator-"):
            out = run([adb_path(), "-s", serial, "emu", "avd", "name"], timeout=10).stdout
            first = out.strip().splitlines()[0].strip() if out.strip() else ""
            names[serial] = first or serial
    return names


def booted(serial: str) -> bool:
    out = run([adb_path(), "-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=10).stdout
    return out.strip() == "1"


def start_emulators(avds: list[str] | None = None) -> list[str]:
    """Launch the configured AVDs that are not running; wait until every emulator has booted."""
    avds = avds or CONFIG["emulators"]["avds"]
    running = set(running_avds().values())
    started = []
    for avd in avds:
        if avd not in running:
            detach([emulator_bin(), "-avd", avd], f"emulator-{avd}")
            started.append(avd)
    deadline = time.time() + CONFIG["emulators"]["boot_timeout"]
    while time.time() < deadline:
        names = running_avds()
        if all(avd in names.values() for avd in avds) and all(booted(s) for s in names):
            break
        time.sleep(4)
    return started


def stop_emulators() -> list[str]:
    stopped = []
    for serial, avd in running_avds().items():
        run([adb_path(), "-s", serial, "emu", "kill"], timeout=20)
        stopped.append(avd)
    return stopped


def appium_ready() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CONFIG['appium']['port']}/status", timeout=3) as response:
            return response.status == 200
    except OSError:
        return False


def start_appium(timeout: float = 60) -> bool:
    if appium_ready():
        return True
    run(["systemctl", "--user", "start", CONFIG["appium"]["unit"]])
    deadline = time.time() + timeout
    while time.time() < deadline:
        if appium_ready():
            return True
        time.sleep(2)
    return False


def stop_appium() -> None:
    run(["systemctl", "--user", "stop", CONFIG["appium"]["unit"]])
