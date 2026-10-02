"""Daily status report: what was tested, written, filed and committed today, as a paste-ready
Markdown update in ~/Documents/DailyReports/<date>.md (also copied to the clipboard).

Sources (read-only): every RUN_STATE.json under TestPlanReport (a verdict counts for the day
its `ts` falls on), case sheets written that day, bug drafts / defect packs / FILED_BUGS.json,
your own commits in every Workspace repo, and the ticket changes the watcher saw.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from workday.common import load_state, notify, run
from workday.config import CONFIG, expand
from workday import gitflow

STATUS_ALIASES = {
    "pass": "Pass", "passed": "Pass",
    "fail": "Fail", "failed": "Fail",
    "blocked": "Blocked",
    "n/a": "N/A", "na": "N/A", "not applicable": "N/A", "notapplicable": "N/A",
}
ORDER = ["Pass", "Fail", "Blocked", "N/A"]


def normalise(status: str) -> str:
    return STATUS_ALIASES.get((status or "").strip().lower(), (status or "").strip() or "Untested")


def _is_on(path: Path, day: date) -> bool:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).date() == day
    except OSError:
        return False


def run_summary(state_file: Path, day: date) -> dict | None:
    """Verdicts given on `day` in one run (None when the run was not touched that day)."""
    try:
        data = json.loads(state_file.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    cases = {k: v for k, v in data.items() if isinstance(v, dict) and "status" in v}
    stamped = [v for v in cases.values() if v.get("ts")]
    if stamped:
        today = [v for v in stamped if str(v["ts"]).startswith(day.isoformat())]
    elif _is_on(state_file, day):
        today = list(cases.values())  # older runs without timestamps
    else:
        today = []
    if not today:
        return None
    counts = Counter(normalise(v.get("status", "")) for v in today)
    totals = Counter(normalise(v.get("status", "")) for v in cases.values())
    defects = sorted({part.strip() for v in today
                      if v.get("defect") and normalise(v.get("status", "")) == "Fail"
                      for part in str(v["defect"]).split(",") if part.strip()})
    folder = state_file.parent
    meta = {}
    try:
        meta = json.loads((folder / "RUN.json").read_text())
    except (OSError, ValueError):
        pass
    return {"folder": folder, "ticket": folder.parent.name, "run": folder.name, "today": counts,
            "total": sum(totals.values()), "totals": totals, "defects": defects,
            "company": meta.get("company", "") if isinstance(meta, dict) else ""}


def _filed_bugs(folder: Path, day: date) -> list[str]:
    path = folder / "FILED_BUGS.json"
    if not _is_on(path, day):
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    items = data if isinstance(data, list) else data.get("bugs", data.get("filed", [])) if isinstance(data, dict) else []
    ids = []
    for item in items:
        if isinstance(item, dict):
            ids.append(str(item.get("id") or item.get("bug_id") or ""))
        else:
            ids.append(str(item))
    return [i for i in ids if i]


def _defect_pack(folder: Path, day: date) -> int:
    for pack in (folder / "bugs" / "bugs.md", folder / "bugs_greybox" / "bugs.md"):
        if _is_on(pack, day):
            return len(re.findall(r"^\|\s*DEF-[\w-]+", pack.read_text(errors="replace"), re.M))
    return 0


def _sheets_written(root: Path, day: date) -> list[tuple[str, str, int]]:
    out = []
    for sheet in root.glob("*/TestCases/*.csv"):
        if not _is_on(sheet, day) or "_qatouch" in sheet.name:
            continue
        try:
            with sheet.open(encoding="utf-8-sig", newline="") as handle:
                rows = sum(1 for _ in csv.DictReader(handle))
        except (OSError, csv.Error, UnicodeDecodeError):
            rows = 0
        out.append((sheet.parent.parent.name, sheet.name, rows))
    return out


def _commits(day: date) -> dict[str, list[str]]:
    email = run(["git", "config", "--global", "user.email"]).stdout.strip()
    since, until = day.isoformat() + " 00:00", (day + timedelta(days=1)).isoformat() + " 00:00"
    out: dict[str, list[str]] = {}
    for repo in gitflow.repos(include_greybox_checkouts=True):
        args = ["log", "--all", "--no-merges", f"--since={since}", f"--until={until}", "--format=%s"]
        if email:
            args.append(f"--author={email}")
        subjects = [s for s in gitflow.git(repo, *args).stdout.splitlines() if s.strip()]
        if subjects:
            out[repo.name] = subjects
    return out


def _short(subject: str, width: int = 140) -> str:
    subject = re.sub(r"^#\S+ - ", "", subject)
    subject = re.sub(r"\s*\[(ai|tool):[^\]]*\]", "", subject)
    if len(subject) > width and ": " in subject[:width]:
        subject = subject.split(": ", 1)[0]  # the headline before the details
    return subject if len(subject) <= width else subject[: width - 1].rstrip() + "…"


def build(day: date) -> str:
    root = expand(CONFIG["paths"]["testplanreport"])
    runs = [r for r in (run_summary(p, day) for p in sorted(root.glob("**/RUN_STATE.json"))
                        if len(p.relative_to(root).parts) <= 4) if r]
    lines = [f"**Daily update - {day.strftime('%d %b %Y (%a)')}**", ""]

    lines.append("**Testing**")
    if not runs:
        lines.append("- No test execution recorded today.")
    for r in runs:
        counts = ", ".join(f"{r['today'][k]} {k.lower() if k != 'N/A' else 'N/A'}" for k in ORDER if r["today"].get(k))
        extra = ", ".join(f"{v} {k.lower()}" for k, v in r["today"].items() if k not in ORDER)
        done = sum(r["totals"].values())
        where = f" on {r['company']}" if r["company"] else ""
        lines.append(f"- {r['ticket']}{where}: executed {sum(r['today'].values())} case(s) today - "
                     f"{counts}{', ' + extra if extra else ''} (run total {done}).")
        if r["defects"]:
            lines.append(f"  - Defects: {', '.join(r['defects'][:12])}")
        filed = _filed_bugs(r["folder"], day)
        if filed:
            lines.append(f"  - Bugs filed in Bugzilla: {', '.join(filed)}")
        pack = _defect_pack(r["folder"], day)
        if pack:
            lines.append(f"  - Defect pack prepared: {pack} defect(s)")
    lines.append("")

    sheets = _sheets_written(root, day)
    if sheets:
        lines.append("**Test cases written**")
        for ticket, name, rows in sheets:
            lines.append(f"- {ticket}: {rows} case(s) ({name})")
        lines.append("")

    changes = [c for c in load_state("watch").get("log", []) if str(c.get("at", "")).startswith(day.isoformat())]
    if changes:
        lines.append("**Tickets**")
        for change in changes[-15:]:
            lines.append(f"- {change['text']}")
        lines.append("")

    commits = _commits(day)
    if commits:
        lines.append("**Automation / tooling**")
        for repo, subjects in commits.items():
            lines.append(f"- {repo}: {len(subjects)} commit(s)")
            for subject in subjects[:5]:
                lines.append(f"  - {_short(subject)}")
        lines.append("")

    lines += ["**Blockers**", "- None", "", "**Plan for tomorrow**", "- "]
    return "\n".join(lines) + "\n"


def write(day: date | None = None, copy: bool = True, announce: bool = True) -> Path:
    day = day or date.today()
    text = build(day)
    folder = expand(CONFIG["report"]["dir"])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{day.isoformat()}.md"
    path.write_text(text)
    copied = False
    if copy and shutil.which("wl-copy"):
        copied = subprocess.run(["wl-copy"], input=text, text=True, capture_output=True,
                                timeout=10, check=False).returncode == 0
    if announce:
        notify("Daily report ready", f"{path}" + (" - copied to clipboard" if copied else ""))
    return path
