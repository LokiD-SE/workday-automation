"""`day start` / `day end` / `day status` - the desk's morning and evening in one command."""

from __future__ import annotations

import time
from datetime import date

from workday import apk, devices, gitflow, health, report, vpn
from workday.common import ask, detach, load_state, run
from workday.config import CONFIG, STATE_DIR, expand

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def step(title: str) -> None:
    print(f"{BOLD}▸ {title}{RESET}", flush=True)


def services(action: str) -> bool:
    helper = CONFIG["services"]["helper"]
    out = run(["sudo", "-n", helper, action], timeout=180)
    if out.returncode == 0:
        print("  " + out.stdout.strip().replace("\n", "\n  "))
        return True
    print(f"  {DIM}password-less helper not installed yet (sudo install-root.sh) - asking sudo{RESET}")
    script = {"start": "~/startenv.sh", "stop": "~/stop.sh"}.get(action)
    if not script:
        return False
    import subprocess
    return subprocess.run(["bash", "-lc", script], check=False).returncode == 0


def greybox_running() -> bool:
    return run(["pgrep", "-f", "greybox-mobithon-2026/main.py"]).returncode == 0


def start(skip: set[str]) -> int:
    began = time.time()
    if "services" not in skip:
        step("Local web stack (mysql, php7.4-fpm, nginx) + cache reset")
        services("start")
    if "vpn" not in skip:
        step("VPN - enter the authenticator code in the popup")
        ok, msg = vpn.up(wait=True)
        print(f"  {'✔' if ok else '✘'} {msg}")
    if "emulators" not in skip:
        step(f"Emulators {', '.join(CONFIG['emulators']['avds'])}")
        started = devices.start_emulators()
        print(f"  started {', '.join(started)}" if started else "  already running")
        for line in apk.process(quiet=True):
            print(f"  apk: {line}")
    if "appium" not in skip:
        step("Appium")
        print("  ready" if devices.start_appium() else "  ✘ did not come up - journalctl --user -u workday-appium")
    if "greybox" not in skip:
        step("Greybox")
        if greybox_running():
            print("  already running")
        else:
            detach([str(expand("~/.local/bin/greybox-launch.sh"))], "greybox")
            print("  launched")
    step("Health")
    checks = health.collect()
    print(health.table(checks))
    watch = load_state("watch")
    ready = [(k, v) for k, v in watch.get("items", {}).items() if v.get("status", "").lower() in
             {"dev done", "ready for qa", "qa ready", "ready for testing"}]
    if watch:
        step(f"Tickets ({len(watch.get('items', {}))} open, watcher last ran {watch.get('last_run', 'never')})")
        ready.sort(key=lambda kv: kv[1].get("updated", 0), reverse=True)
        for key, item in ready[:10]:
            when = time.strftime("%d %b", time.localtime(item["updated"] / 1000)) if item.get("updated") else ""
            print(f"  {key:<12} {item['status']:<10} {when:<7} {item['name'][:80]}")
        if len(ready) > 10:
            print(f"  … {len(ready) - 10} older ones ready for QA (day watch --show for changes)")
    bad = health.failures(checks)
    print(f"\n{'All green' if not bad else f'{len(bad)} problem(s): ' + ', '.join(c.name for c in bad)}"
          f" - {time.time() - began:.0f}s")
    (STATE_DIR).mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "last_start").write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
    return 0 if not bad else 1


def end(assume_shutdown: bool | None = None) -> int:
    step("Git - repos with uncommitted or unpushed work")
    pending = [st for st in (gitflow.status(r) for r in gitflow.repos()) if not st.clean]
    if not pending:
        print("  everything committed and pushed")
    for st in pending:
        print(f"  {st.path.name}: {st.dirty} changed file(s), {st.ahead} unpushed commit(s) on {st.branch}")
        if ask(f"  gsync {st.path.name} now?"):
            gitflow.sync(st.path)
    step("Daily report")
    path = report.write(copy=True)
    print(f"  {path}  (copied to clipboard)")
    print("  " + "\n  ".join(path.read_text().splitlines()[:14]))
    if date.today().weekday() == 4 and ask("Friday - run the weekly Drive backup now (instead of 17:00 timer)?"):
        from workday import backup
        backup.execute()
    step("Stopping Appium, emulators, web stack, VPN")
    devices.stop_appium()
    stopped = devices.stop_emulators()
    print(f"  emulators stopped: {', '.join(stopped) or 'none'}")
    services("stop")
    ok, msg = vpn.down()
    print(f"  vpn: {msg}")
    shutdown = assume_shutdown if assume_shutdown is not None else ask("Shut down now?")
    if shutdown:
        run(["systemctl", "poweroff"])
    return 0


def status() -> int:
    print(health.table(health.collect()))
    watch = load_state("watch")
    print(f"\nwatcher: last run {watch.get('last_run', 'never')}, {len(watch.get('items', {}))} open item(s)"
          + (f", error: {watch['last_error']}" if watch.get("last_error") else ""))
    timers = run(["systemctl", "--user", "list-timers", "workday-*", "--no-pager"]).stdout.strip()
    print("\n" + timers)
    return 0
