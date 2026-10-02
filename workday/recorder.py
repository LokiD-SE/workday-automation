"""Run recorder (workday-recorder.service): evidence for app runs without anyone asking.

A run is *active* while its RUN_STATE.json (qa-run's state) changed in the last
recorder.idle_minutes. For an active run and each emulator / device online:
- logcat (`-v threadtime`, from now on) -> <run>/evidence/logs/logcat_<serial>_<time>.txt,
  a new file every 50 MB;
- a rolling screen recording: 30 s segments on the device, the last 4 kept;
- when a case turns Failed in RUN_STATE.json: the last 3 segments (~90 s up to the failure,
  after the current one finishes) -> <run>/evidence/recordings/<case>_<serial>_<n>.mp4 and
  the crash buffer -> <run>/evidence/logs/<case>_<serial>_crash.txt.
Nothing is written into RUN_STATE.json; qa-run and Greybox stay the owners of the run.
"""

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from workday.common import adb_devices, adb_path, notify, run
from workday.config import CONFIG, expand
from workday.report import normalise

LOGCAT_MAX = 50 * 1024 * 1024
SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def state_files(root: Path) -> list[Path]:
    return list(root.glob("*/Execution_*/RUN_STATE.json")) + list(root.glob("*/RUN_STATE.json"))


def active_run(root: Path, idle_minutes: float, now: float | None = None) -> Path | None:
    now = now or time.time()
    newest, newest_mtime = None, 0.0
    for path in state_files(root):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if now - mtime <= idle_minutes * 60 and mtime > newest_mtime:
            newest, newest_mtime = path.parent, mtime
    return newest


def statuses(run_folder: Path) -> dict[str, str]:
    try:
        data = json.loads((run_folder / "RUN_STATE.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: normalise(v.get("status", "")) for k, v in data.items() if isinstance(v, dict) and "status" in v}


def newly_failed(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return [case for case, status in after.items() if status == "Fail" and before.get(case) != "Fail"]


class DeviceRecorder:
    def __init__(self, serial: str, run_folder: Path, settings: dict):
        self.serial = serial
        self.run_folder = run_folder
        self.settings = settings
        self.stop_event = threading.Event()
        self.segments: deque[tuple[int, float, float]] = deque(maxlen=settings["keep_segments"])
        self.segment_done = threading.Condition()
        self.logcat: subprocess.Popen | None = None
        self.logcat_path: Path | None = None
        self.threads: list[threading.Thread] = []
        self.since = ""

    @property
    def tag(self) -> str:
        return SAFE.sub("_", self.serial)

    def adb(self, *args: str) -> list[str]:
        return [adb_path(), "-s", self.serial, *args]

    def start(self) -> None:
        stamp = run(self.adb("shell", "date '+%m-%d %H:%M:%S.000'"), timeout=10).stdout.strip()
        self.since = stamp or time.strftime("%m-%d %H:%M:%S.000")
        self._start_logcat()
        self.threads.append(threading.Thread(target=self._logcat_rotator, daemon=True))
        if self.settings["record_screen"]:
            self.threads.append(threading.Thread(target=self._screen_loop, daemon=True))
        for thread in self.threads:
            thread.start()

    def _start_logcat(self) -> None:
        folder = self.run_folder / "evidence" / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        self.logcat_path = folder / f"logcat_{self.tag}_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        handle = open(self.logcat_path, "ab")
        self.logcat = subprocess.Popen(self.adb("logcat", "-v", "threadtime", "-T", "1"),
                                       stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

    def _logcat_rotator(self) -> None:
        while not self.stop_event.wait(30):
            if self.logcat and self.logcat.poll() is not None:
                self._start_logcat()  # device restarted / adb dropped
            elif self.logcat_path and self.logcat_path.exists() and self.logcat_path.stat().st_size > LOGCAT_MAX:
                self.logcat.terminate()
                self._start_logcat()

    def _screen_loop(self) -> None:
        seconds = int(self.settings["segment_seconds"])
        index = 0
        while not self.stop_event.is_set():
            slot = index % self.settings["keep_segments"]
            started = time.time()
            out = run(self.adb("shell", "screenrecord", "--time-limit", str(seconds), "--bit-rate",
                               str(self.settings["bit_rate"]), f"/sdcard/wd_rec_{slot}.mp4"), timeout=seconds + 20)
            if out.returncode not in (0, 130) and time.time() - started < 3:
                # screenrecord unavailable (or device gone) - don't spin
                if self.stop_event.wait(30):
                    break
                continue
            with self.segment_done:
                self.segments.append((slot, started, time.time()))
                self.segment_done.notify_all()
            index += 1

    def save_failure(self, case: str) -> list[Path]:
        saved: list[Path] = []
        name = SAFE.sub("_", case)
        # crash buffer since recording began on this device (device clock, logcat's -t <time> form)
        crash = run(self.adb("logcat", "-b", "crash", "-d", "-t", self.since), timeout=30).stdout.strip()
        crash = "\n".join(l for l in crash.splitlines() if not l.startswith("--------- beginning of"))
        if crash:
            path = self.run_folder / "evidence" / "logs" / f"{name}_{self.tag}_crash.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(crash + "\n")
            saved.append(path)
        if not self.settings["record_screen"]:
            return saved
        with self.segment_done:  # include the segment being recorded at the failure
            count = len(self.segments)
            self.segment_done.wait_for(lambda: len(self.segments) > count or self.stop_event.is_set(),
                                       timeout=self.settings["segment_seconds"] + 25)
            chosen = list(self.segments)[-self.settings["pull_segments"]:]
        folder = self.run_folder / "evidence" / "recordings"
        folder.mkdir(parents=True, exist_ok=True)
        for number, (slot, _, _) in enumerate(chosen, 1):
            target = folder / f"{name}_{self.tag}_{number}.mp4"
            if run(self.adb("pull", f"/sdcard/wd_rec_{slot}.mp4", str(target)), timeout=120).returncode == 0:
                saved.append(target)
        return saved

    def stop(self) -> None:
        self.stop_event.set()
        with self.segment_done:
            self.segment_done.notify_all()
        if self.logcat and self.logcat.poll() is None:
            self.logcat.terminate()
        run(self.adb("shell", "pkill", "-INT", "-f", "wd_rec_"), timeout=10)


class Recorder:
    def __init__(self):
        self.settings = CONFIG["recorder"]
        self.root = expand(CONFIG["paths"]["testplanreport"])
        self.run_folder: Path | None = None
        self.devices: dict[str, DeviceRecorder] = {}
        self.last_statuses: dict[str, str] = {}

    def stop_all(self) -> None:
        for recorder in self.devices.values():
            recorder.stop()
        self.devices.clear()

    def on_failure(self, case: str) -> None:
        def work(recorder: DeviceRecorder) -> None:
            saved = recorder.save_failure(case)
            if saved:
                notify(f"Evidence saved for {case}", f"{recorder.serial}: " + ", ".join(p.name for p in saved), "low")
        for recorder in list(self.devices.values()):
            threading.Thread(target=work, args=(recorder,), daemon=True).start()

    def tick(self) -> None:
        current = active_run(self.root, self.settings["idle_minutes"])
        if current != self.run_folder:
            self.stop_all()
            self.run_folder = current
            self.last_statuses = statuses(current) if current else {}
            if current:
                print(f"active run: {current}", flush=True)
        if not self.run_folder:
            return
        serials = set(adb_devices())
        for serial in serials - set(self.devices):
            recorder = DeviceRecorder(serial, self.run_folder, self.settings)
            recorder.start()
            self.devices[serial] = recorder
            print(f"recording {serial}", flush=True)
        for serial in set(self.devices) - serials:
            self.devices.pop(serial).stop()
        now = statuses(self.run_folder)
        for case in newly_failed(self.last_statuses, now):
            print(f"failed: {case}", flush=True)
            self.on_failure(case)
        self.last_statuses = now


def main() -> int:
    recorder = Recorder()

    def finish(*_):
        recorder.stop_all()
        sys.exit(0)

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    while True:
        try:
            recorder.tick()
        except Exception as exc:  # noqa: BLE001 - keep the service alive
            print(f"tick error: {exc}", file=sys.stderr, flush=True)
        time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
