"""gst (status of every repo) and gsync (commit -> pull --rebase -> push), plus the pre-push gate.

gsync never commits something that looks like a secret (.env, keys, .ovpn, credentials) and
never resolves a conflict for you: a failed rebase is aborted, the repo is left as it was.
The pre-push hook runs the repo's tests (config git.test_commands); SKIP_TESTS=1 skips them.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from workday.common import ask, run
from workday.config import CONFIG, expand

SECRET_RE = re.compile(r"(^|/)(\.env(\..*)?|.*\.pem|.*\.key|id_rsa.*|.*\.ovpn|\.git-credentials|.*credentials.*\.json|vpn-credentials)$")
PREFIX_RE = re.compile(r"^(#\S+ - )")


@dataclass
class RepoStatus:
    path: Path
    branch: str
    dirty: int
    ahead: int
    behind: int
    upstream: bool

    @property
    def clean(self) -> bool:
        return self.dirty == 0 and self.ahead == 0


def git(repo: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(repo), *args], timeout=timeout)


def repos(include_greybox_checkouts: bool = False) -> list[Path]:
    workspace = expand(CONFIG["paths"]["workspace"])
    found = sorted(p.parent for p in workspace.glob("*/.git"))
    if include_greybox_checkouts:
        greybox = expand(CONFIG["paths"]["greybox"])
        found += [greybox / rel for rel in CONFIG["watch"]["repos"] if (greybox / rel / ".git").exists()]
    return found


def status(repo: Path, fetch: bool = False) -> RepoStatus:
    if fetch:
        git(repo, "fetch", "--quiet", timeout=120)
    branch = git(repo, "branch", "--show-current").stdout.strip() or "(detached)"
    dirty = len([l for l in git(repo, "status", "--porcelain").stdout.splitlines() if l.strip()])
    counts = git(repo, "rev-list", "--left-right", "--count", "@{u}...HEAD")
    upstream = counts.returncode == 0
    behind, ahead = (int(x) for x in counts.stdout.split()) if upstream else (0, 0)
    return RepoStatus(repo, branch, dirty, ahead, behind, upstream)


def gst_main(argv: list[str]) -> int:
    fetch = "-f" in argv or "--fetch" in argv
    every = "-a" in argv or "--all" in argv
    home = Path.home()
    print(f"{'repo':<44} {'branch':<28} {'dirty':>5} {'ahead':>5} {'behind':>6}")
    for repo in repos(include_greybox_checkouts=every):
        st = status(repo, fetch=fetch)
        if st.clean and st.behind == 0 and "-q" in argv:
            continue
        name = str(repo).replace(str(home), "~")
        flag = "" if st.upstream else "  (no upstream)"
        print(f"{name:<44} {st.branch[:28]:<28} {st.dirty:>5} {st.ahead:>5} {st.behind:>6}{flag}")
    return 0


def suggest_prefix(repo: Path) -> str:
    subject = git(repo, "log", "-1", "--format=%s").stdout.strip()
    match = PREFIX_RE.match(subject)
    return match.group(1) if match else ""


def sync(repo: Path, message: str | None = None, assume_yes: bool = False, push: bool = True) -> bool:
    repo = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip() or repo)
    print(f"\n== {repo}")
    changes = [l for l in git(repo, "status", "--porcelain").stdout.splitlines() if l.strip()]
    if changes:
        print("\n".join("   " + line for line in changes[:40]) + ("\n   …" if len(changes) > 40 else ""))
        secrets = [line[3:] for line in changes if SECRET_RE.search(line[3:].strip('"'))]
        if secrets:
            print(f"   refusing: looks like secrets, add them to .gitignore first: {', '.join(secrets)}")
            return False
        if not (assume_yes or ask(f"Commit these {len(changes)} change(s)?")):
            print("   skipped commit")
        else:
            if not message:
                prefix = suggest_prefix(repo)
                if not sys.stdin.isatty():
                    print("   no message given (-m) and no terminal - skipped")
                    return False
                text = input(f"   message: {prefix}").strip()
                if not text:
                    print("   empty message - skipped")
                    return False
                message = prefix + text
            git(repo, "add", "-A")
            out = git(repo, "commit", "-m", message)
            if out.returncode != 0:
                print("   commit failed:\n" + (out.stdout + out.stderr).strip())
                return False
            print(f"   committed: {message}")
    st = status(repo)
    if not st.upstream:
        print(f"   branch {st.branch} has no upstream - push it yourself with `git push -u origin {st.branch}`")
        return True
    pulled = git(repo, "pull", "--rebase", "--autostash", timeout=300)
    if pulled.returncode != 0:
        git(repo, "rebase", "--abort")
        print("   pull --rebase failed (conflict?) - rebase aborted, nothing pushed:\n"
              + (pulled.stdout + pulled.stderr).strip()[-800:])
        return False
    st = status(repo)
    if st.ahead == 0:
        print("   up to date, nothing to push")
        return True
    if not push or not (assume_yes or ask(f"Push {st.ahead} commit(s) on {st.branch}?", default=True)):
        print("   not pushed")
        return True
    # Not captured: the pre-push hook's test output should stream to the terminal.
    pushed = subprocess.run(["git", "-C", str(repo), "push"], check=False)
    print("   pushed" if pushed.returncode == 0 else "   push failed (tests or remote) - see above")
    return pushed.returncode == 0


def gsync_main(argv: list[str]) -> int:
    message, yes, targets = None, False, []
    args = iter(argv)
    for arg in args:
        if arg in {"-m", "--message"}:
            message = next(args, None)
        elif arg in {"-y", "--yes"}:
            yes = True
        else:
            targets.append(Path(arg).expanduser())
    targets = targets or [Path.cwd()]
    ok = all([sync(target, message=message, assume_yes=yes) for target in targets])
    return 0 if ok else 1


def pre_push_main() -> int:
    """Called by hooks/pre-push inside the repo being pushed."""
    if os.environ.get("SKIP_TESTS") == "1":
        print("pre-push: SKIP_TESTS=1 - tests skipped")
        return 0
    top = Path(run(["git", "rev-parse", "--show-toplevel"]).stdout.strip())
    command = CONFIG["git"]["test_commands"].get(top.name)
    if not command:
        return 0
    print(f"pre-push: {command}", flush=True)
    result = subprocess.run(shlex.split(command), cwd=top, check=False)
    if result.returncode != 0:
        print("pre-push: tests failed - push stopped (SKIP_TESTS=1 git push to override)")
    return result.returncode
