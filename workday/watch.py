"""Ticket + PR watcher (every 30 min in work hours, workday-watch.timer). Read-only.

- Zoho: your open tasks (/mytasks/?status=open) and issues (/mybugs/) through Greybox's
  GET-only ZohoClient - a new assignment or a status change is a notification; a move to a
  "ready for QA" status (Dev Done ...) is an urgent one.
- Code: `git fetch` in Greybox's three checkouts, then new commits / branches whose subject
  or name carries one of your open ticket keys.
- Optional: config watch.on_new_task (e.g. "qa-run generate {key}") runs once per new task.

Runs under Greybox's venv (bin/day re-execs itself there). Nothing is written to Zoho.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import time

from workday.common import detach, load_state, notify, run, save_state
from workday.config import CONFIG, expand

READY_STATUSES = {"dev done", "ready for qa", "qa ready", "ready for testing", "deployed to staging"}
MAX_SEEN = 3000
KEY_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{0,11}-[TI]\d{1,7})(?!\d)")


def _zoho():
    greybox = expand(CONFIG["paths"]["greybox"])
    if str(greybox) not in sys.path:
        sys.path.insert(0, str(greybox))
    from dotenv import load_dotenv  # Greybox's venv
    load_dotenv(greybox / ".env")
    from graph import zoho_client
    if not zoho_client.configured():
        raise RuntimeError("Zoho is not configured in Greybox's .env")
    return zoho_client, zoho_client.ZohoClient()


def _status(item: dict) -> str:
    status = item.get("status")
    if isinstance(status, dict):
        return status.get("name") or status.get("type") or ""
    return str(status or "")


def fetch_items() -> dict[str, dict]:
    module, client = _zoho()
    portal = client.portal_id()
    items: dict[str, dict] = {}
    index = 1
    while True:
        page = client._get(f"/restapi/portal/{portal}/mytasks/", {"index": index, "range": 100, "status": "open"})
        tasks = page.get("tasks", []) if isinstance(page, dict) else []
        for task in tasks:
            project = (task.get("project") or {}).get("id_string") or (task.get("project") or {}).get("id", "")
            items[task.get("key", task.get("id_string"))] = {
                "kind": "task", "name": task.get("name", ""), "status": _status(task),
                "updated": int(task.get("last_updated_time_long") or 0),
                "url": f"{module.WEB_ROOT}/portal/{client.portal}#zp/projects/{project}/task-detail/{task.get('id_string', '')}"}
        index += 100
        if len(tasks) < 100 or index > 5000:
            break
    try:
        page = client._get(f"/restapi/portal/{portal}/mybugs/", {"index": 1, "range": 100})
        for bug in page.get("bugs", []) if isinstance(page, dict) else []:
            status = _status(bug)
            if status.lower() in CONFIG["watch"]["closed_statuses"]:
                continue
            project = (bug.get("project") or {}).get("id_string") or (bug.get("project") or {}).get("id", "")
            items[bug.get("key", bug.get("id_string"))] = {
                "kind": "issue", "name": bug.get("title", ""), "status": status,
                "url": f"{module.WEB_ROOT}/portal/{client.portal}#zp/projects/{project}/issue-detail/{bug.get('id_string', '')}"}
    except Exception as exc:  # noqa: BLE001 - issues are a bonus; tasks already read
        print(f"mybugs: {exc}", file=sys.stderr)
    return items


def diff_items(old: dict, new: dict, first_run: bool) -> list[tuple[str, str, str]]:
    """(urgency, title, body) events between two snapshots."""
    if first_run:
        return []
    events = []
    for key, item in new.items():
        before = old.get(key)
        if before is None:
            events.append(("normal", f"New {item['kind']} assigned: {key}", f"{item['name']}\n{item['status']}"))
        elif before.get("status") != item["status"]:
            ready = item["status"].lower() in READY_STATUSES
            events.append(("critical" if ready else "normal",
                           f"{key}: {before.get('status')} → {item['status']}" + (" - ready for QA" if ready else ""),
                           item["name"]))
    for key in set(old) - set(new):
        events.append(("low", f"{key} closed / unassigned", old[key].get("name", "")))
    return events


def key_pattern(keys) -> re.Pattern | None:
    keys = sorted({k for k in keys if k}, key=len, reverse=True)
    if not keys:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(k) for k in keys) + r")(?!\d)")


def scan_repos(pattern: re.Pattern, state: dict) -> list[tuple[str, str, str]]:
    greybox = expand(CONFIG["paths"]["greybox"])
    seen = set(state.get("commits_seen", []))
    branches_seen = set(state.get("branches_seen", []))
    first = not seen and not branches_seen
    since = state.get("last_commit_check") or time.strftime("%Y-%m-%d", time.localtime(time.time() - 3 * 86400))
    events: list[tuple[str, str, str]] = []
    grouped: dict[tuple[str, str], list[str]] = {}
    for rel in CONFIG["watch"]["repos"]:
        repo = greybox / rel
        if not (repo / ".git").exists():
            continue
        name = repo.name if rel != "Repositories" else "bizom-laravel"
        run(["git", "-C", str(repo), "fetch", "--quiet", "origin"], timeout=240)
        log = run(["git", "-C", str(repo), "log", "--remotes", "--no-merges", f"--since={since}",
                   "--format=%H\x1f%an\x1f%s"], timeout=60).stdout
        for line in log.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 3 or parts[0] in seen:
                continue
            seen.add(parts[0])
            for key in set(pattern.findall(parts[2])):
                grouped.setdefault((key, name), []).append(f"{parts[1]}: {parts[2][:90]}")
        refs = run(["git", "-C", str(repo), "for-each-ref", "refs/remotes", "--format=%(refname:short)"], timeout=30).stdout
        for ref in refs.split():
            tag = f"{name}:{ref}"
            if tag in branches_seen:
                continue
            branches_seen.add(tag)
            match = pattern.search(ref)
            if match and not first:
                events.append(("normal", f"{match.group(1)}: new branch in {name}", ref))
    if not first:
        for (key, repo_name), commits in grouped.items():
            events.append(("normal", f"{key}: {len(commits)} new commit(s) in {repo_name}", "\n".join(commits[:4])))
    state["commits_seen"] = list(seen)[-MAX_SEEN:]
    state["branches_seen"] = sorted(branches_seen)
    state["last_commit_check"] = time.strftime("%Y-%m-%d %H:%M")
    return events


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    state = load_state("watch")
    first_run = "items" not in state
    try:
        items = fetch_items()
    except Exception as exc:  # noqa: BLE001
        print(f"Zoho read failed: {exc}", file=sys.stderr)
        state["last_error"] = f"{time.strftime('%Y-%m-%d %H:%M')} {exc}"
        save_state("watch", state)
        return 1
    events = diff_items(state.get("items", {}), items, first_run)
    new_tasks = [k for k, v in items.items() if k not in state.get("items", {}) and v["kind"] == "task"]
    state["items"] = items
    # QA tasks are titled "QA - PR1-T11635 - ..."; the product key is what commits carry.
    pattern = key_pattern(set(items) | {k for v in items.values() for k in KEY_RE.findall(v.get("name", ""))})
    if pattern and "--no-git" not in argv:
        events += scan_repos(pattern, state)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    state["log"] = (state.get("log", []) + [{"at": stamp, "text": title} for _, title, _ in events])[-400:]
    state["last_run"] = stamp
    state.pop("last_error", None)
    save_state("watch", state)

    if first_run:
        notify("Ticket watcher on", f"Watching {len(items)} open item(s) and their commits.")
    for urgency, title, body in events[:12]:
        notify(title, body, urgency)
    if len(events) > 12:
        notify(f"{len(events) - 12} more ticket / code changes", "See `day watch --show`.")
    hook = CONFIG["watch"].get("on_new_task")
    if hook and not first_run:
        for key in new_tasks:
            detach(["bash", "-lc", hook.format(key=shlex.quote(key))], f"on-new-task-{key}")
    for _, title, body in events:
        print(f"{title} | {body.splitlines()[0] if body else ''}")
    print(f"{len(items)} open item(s), {len(events)} change(s)" + (" (baseline)" if first_run else ""))
    return 0


def show() -> int:
    state = load_state("watch")
    print(f"last run {state.get('last_run', 'never')}" + (f" - error: {state['last_error']}" if state.get("last_error") else ""))
    for entry in state.get("log", [])[-30:]:
        print(f"{entry['at']}  {entry['text']}")
    return 0


def ready_for_qa() -> list[tuple[str, dict]]:
    items = load_state("watch").get("items", {})
    return sorted(((k, v) for k, v in items.items() if v.get("status", "").lower() in READY_STATUSES))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main(sys.argv[1:]))
