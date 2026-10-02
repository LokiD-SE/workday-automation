"""Weekly Google Drive backup, then local clean-up (workday-backup.timer, Fridays 17:00).

1. Upload (rclone copy, never sync - nothing on Drive is ever deleted): TestPlanReport,
   Downloads/SS, Downloads/bugs and the bizom*.apk builds, into backup.remote.
2. Clear locally only what is stale: a top-level entry (a ticket folder, a file) whose newest
   file is older than backup.stale_days, and APKs beyond the newest backup.keep_newest_apks
   that are that old. Each one is deleted only after `rclone check --one-way` confirms every
   file of it is on Drive with the same checksum; anything that does not match is kept.
3. Crash / heap dumps (*.hprof, hs_err logs) are deleted without upload.
A manifest of what was uploaded, deleted and kept goes to ~/.local/state/workday/backup/ and
to <remote>/_manifests/.
"""

from __future__ import annotations

import fcntl
import glob
import io
import json
import os
import shutil
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from workday.common import human_bytes, notify, run
from workday.config import CONFIG, HOME, STATE_DIR, expand

RCLONE_URL = "https://downloads.rclone.org/rclone-current-linux-amd64.zip"
LOCAL_BIN = HOME / ".local" / "bin"
DAY = 86400


@dataclass
class Unit:
    path: str
    remote: str        # remote path of this unit
    is_dir: bool
    size: int
    newest: float      # newest mtime inside
    stale: bool
    reason: str = ""


@dataclass
class Plan:
    copies: list[tuple[str, str, list[str]]] = field(default_factory=list)  # (local, remote, extra args)
    units: list[Unit] = field(default_factory=list)
    crash: list[tuple[str, int]] = field(default_factory=list)

    @property
    def to_delete(self) -> list[Unit]:
        return [u for u in self.units if u.stale]


def rclone() -> str | None:
    found = shutil.which("rclone")
    if found:
        return found
    local = LOCAL_BIN / "rclone"
    return str(local) if local.exists() else None


def remote_name() -> str:
    return CONFIG["backup"]["remote"].split(":", 1)[0] + ":"


def remote_configured() -> bool:
    tool = rclone()
    return bool(tool) and remote_name() in run([tool, "listremotes"], timeout=20).stdout.split()


def tree_stats(path: Path) -> tuple[int, float]:
    """(total bytes, newest mtime) of a file or a folder tree; symlinks not followed."""
    if path.is_symlink() or path.is_file():
        stat = path.lstat()
        return stat.st_size, stat.st_mtime
    size, newest = 0, path.lstat().st_mtime
    for root, dirs, files in os.walk(path):
        for name in files:
            try:
                stat = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            size += stat.st_size
            newest = max(newest, stat.st_mtime)
    return size, newest


def build_plan(now: float | None = None) -> Plan:
    now = now or time.time()
    cfg = CONFIG["backup"]
    remote = cfg["remote"].rstrip("/")
    cutoff = now - cfg["stale_days"] * DAY
    plan = Plan()
    for local_s, dest in cfg["folders"].values():
        local = expand(local_s)
        if not local.is_dir():
            continue
        target = f"{remote}/{dest}"
        plan.copies.append((str(local), target, ["--create-empty-src-dirs"]))
        for entry in sorted(local.iterdir()):
            if entry.is_symlink():
                continue
            size, newest = tree_stats(entry)
            plan.units.append(Unit(str(entry), f"{target}/{entry.name}", entry.is_dir(), size, newest,
                                   stale=newest < cutoff))
    apks = sorted((Path(p) for p in glob.glob(str(expand(cfg["apk_glob"])))), key=lambda p: p.stat().st_mtime)
    if apks:
        folder = apks[0].parent
        target = f"{remote}/{cfg['apk_remote']}"
        plan.copies.append((str(folder), target, ["--max-depth", "1", "--include", Path(cfg["apk_glob"]).name]))
        keep = set(apks[-cfg["keep_newest_apks"]:]) if cfg["keep_newest_apks"] else set()
        for apk in apks:
            size, newest = tree_stats(apk)
            plan.units.append(Unit(str(apk), f"{target}/{apk.name}", False, size, newest,
                                   stale=apk not in keep and newest < cutoff,
                                   reason="newest kept" if apk in keep else ""))
    for pattern in cfg["crash_globs"]:
        for name in glob.glob(str(expand(pattern))):
            path = Path(name)
            if path.is_file() and now - path.stat().st_mtime > 3600:
                plan.crash.append((name, path.stat().st_size))
    return plan


def print_plan(plan: Plan) -> None:
    print("Upload (rclone copy):")
    for local, remote, _ in plan.copies:
        print(f"  {local}  ->  {remote}")
    stale = plan.to_delete
    print(f"\nClear locally after verification ({len(stale)} item(s), {human_bytes(sum(u.size for u in stale))}):")
    for unit in stale:
        age = (time.time() - unit.newest) / DAY
        print(f"  {human_bytes(unit.size):>10}  {age:4.0f} d  {unit.path}")
    kept = [u for u in plan.units if not u.stale]
    print(f"\nKept locally (recent or newest APKs): {len(kept)} item(s), {human_bytes(sum(u.size for u in kept))}")
    print(f"\nCrash dumps deleted without upload: {len(plan.crash)}")
    for name, size in plan.crash:
        print(f"  {human_bytes(size):>10}  {name}")


def _verify(tool: str, unit: Unit) -> tuple[bool, str]:
    if unit.is_dir:
        out = run([tool, "check", unit.path, unit.remote, "--one-way", "--skip-links"], timeout=3600)
    else:
        with tempfile.NamedTemporaryFile("w", delete=False) as listing:
            listing.write(Path(unit.path).name + "\n")
        parent_remote = unit.remote.rsplit("/", 1)[0]
        out = run([tool, "check", str(Path(unit.path).parent), parent_remote, "--one-way",
                   "--files-from-raw", listing.name], timeout=3600)
        os.unlink(listing.name)
    tail = (out.stderr or out.stdout).strip().splitlines()[-1:] or [""]
    return out.returncode == 0, tail[0][:200]


def _delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def execute(dry_run: bool = False) -> int:
    if dry_run:
        print_plan(build_plan())
        if not remote_configured():
            print("\n(rclone / Drive remote not set up yet - `day backup setup`)")
        return 0
    tool = rclone()
    if not tool or not remote_configured():
        msg = "rclone or the Drive remote is not set up - run `day backup setup`"
        print(msg)
        notify("Backup skipped", msg, "critical")
        return 1
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / "backup.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another backup is running")
        return 1
    plan = build_plan()
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    manifest: dict = {"started": started, "remote": CONFIG["backup"]["remote"], "uploaded": [], "deleted": [],
                      "kept_unverified": [], "crash_deleted": [], "errors": []}
    failed_sources: set[str] = set()
    for local, remote, extra in plan.copies:
        print(f"upload {local} -> {remote}", flush=True)
        out = run([tool, "copy", local, remote, "--skip-links", "--transfers", "4", "--checkers", "8",
                   "--stats-one-line", "--stats", "0", *extra], timeout=6 * 3600)
        if out.returncode != 0:
            failed_sources.add(local)
            manifest["errors"].append({"source": local, "error": out.stderr.strip()[-500:]})
            print(f"  upload failed: {out.stderr.strip()[-300:]}")
        else:
            manifest["uploaded"].append({"source": local, "remote": remote})
    freed = 0
    for unit in plan.to_delete:
        if str(Path(unit.path).parent) in failed_sources:
            manifest["kept_unverified"].append({**asdict(unit), "why": "upload of its folder failed"})
            continue
        ok, detail = _verify(tool, unit)
        if not ok:
            manifest["kept_unverified"].append({**asdict(unit), "why": detail})
            print(f"  kept (not verified): {unit.path} - {detail}")
            continue
        try:
            _delete(Path(unit.path))
        except OSError as exc:
            manifest["kept_unverified"].append({**asdict(unit), "why": f"delete failed: {exc}"})
            continue
        freed += unit.size
        manifest["deleted"].append(asdict(unit))
        print(f"  cleared {unit.path} ({human_bytes(unit.size)})")
    for name, size in plan.crash:
        try:
            os.unlink(name)
            freed += size
            manifest["crash_deleted"].append({"path": name, "size": size})
        except OSError as exc:
            manifest["errors"].append({"crash": name, "error": str(exc)})
    manifest["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    manifest["freed_bytes"] = freed
    folder = STATE_DIR / "backup"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"manifest_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(manifest, indent=1))
    run([tool, "copyto", str(path), f"{CONFIG['backup']['remote'].rstrip('/')}/_manifests/{path.name}"], timeout=300)
    summary = (f"Uploaded {len(manifest['uploaded'])} folder(s); cleared {len(manifest['deleted'])} item(s) "
               f"+ {len(manifest['crash_deleted'])} dump(s), freed {human_bytes(freed)}.")
    if manifest["kept_unverified"] or manifest["errors"]:
        summary += f" {len(manifest['kept_unverified'])} kept (not verified), {len(manifest['errors'])} error(s)."
    print(summary + f"\nmanifest: {path}")
    notify("Weekly backup done" if not manifest["errors"] else "Weekly backup finished with errors", summary,
           "normal" if not manifest["errors"] else "critical")
    return 0 if not manifest["errors"] else 1


def setup() -> int:
    tool = rclone()
    if not tool:
        print(f"installing rclone into {LOCAL_BIN} …")
        with urllib.request.urlopen(RCLONE_URL, timeout=120) as response:
            data = response.read()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            member = next(n for n in archive.namelist() if n.endswith("/rclone"))
            LOCAL_BIN.mkdir(parents=True, exist_ok=True)
            target = LOCAL_BIN / "rclone"
            target.write_bytes(archive.read(member))
            target.chmod(0o755)
        tool = str(target)
        print(run([tool, "version"]).stdout.splitlines()[0])
    if remote_configured():
        print(f"remote {remote_name()} already configured")
    else:
        name = remote_name()[:-1]
        print(f"Creating Google Drive remote '{name}' - a browser window opens; sign in with the Google account "
              "the backup should go to. Scope drive.file: rclone only sees the files it creates.")
        import subprocess
        result = subprocess.run([tool, "config", "create", name, "drive", "scope=drive.file"], check=False)
        if result.returncode != 0 or not remote_configured():
            print("remote not created")
            return 1
    print(f"OK - test with: day backup plan   (Drive folder: {CONFIG['backup']['remote']})")
    return 0
