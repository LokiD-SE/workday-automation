# workday-automation

Desk automation for Bizom QA. Stdlib Python + systemd user units. It never edits Greybox's files
and never writes to Zoho or Bugzilla.

| | What | How it runs |
|---|---|---|
| A | `day start` / `day end` / `day status` | by hand, or *Day Start* / *Day End* in Activities |
| B | VPN with auto-reconnect + authenticator-code popup | `bizom-vpn.service` (root) + `workday-vpn-agent.service` |
| C | New `bizom*.apk` in ~/Downloads → `adb install -r` on every running emulator | `workday-apk.path` |
| D | Zoho tasks/issues assigned to you + commits/branches naming them → notifications | `workday-watch.timer` (Mon–Sat 09:00–20:30, every 30 min) |
| E | Daily report → `~/Documents/DailyReports/<date>.md` + clipboard | `workday-report.timer` (Mon–Sat 18:30), `day end` |
| F | logcat + rolling screen recording during an active qa-run run; on Fail: last ~90 s video + crash log into `evidence/` | `workday-recorder.service` |
| G | `gst`, `gsync`, pre-push test gate in Greybox | by hand / git |
| H | Weekly Drive backup, then clear verified items stale for 14+ days; crash dumps deleted | `workday-backup.timer` (Fri 17:00, catches up after a missed run) |

## Setup

```sh
./install.sh                         # user part (done)
sudo ./install-root.sh               # bizom-env helper, bizom-vpn.service, narrow sudoers rule
day vpn setup                        # VPN username + password -> GNOME keyring
day backup setup                     # rclone + "gdrive" remote (browser sign-in, drive.file scope)
day backup plan                      # dry run: what would be uploaded / cleared
```

## Everyday

```sh
day start                    # stack + cache reset, VPN (popup), emulators, pending APK, Appium, Greybox, health, tickets
day start --skip vpn,greybox
day end                      # git per repo (asks), report, optional Friday backup, stop all, asks to shut down
day watch --show             # recent ticket / code changes
day report --print           # today's report to the terminal
gst -f                       # all repos, after fetch
gsync -m "message"           # in a repo: commit -> pull --rebase -> push (tests run in pre-push)
SKIP_TESTS=1 git push        # bypass the test gate once
```

Settings: `~/.config/workday/config.toml` (defaults in `workday/config.py`). State and manifests:
`~/.local/state/workday/`. Logs: `journalctl --user -u 'workday-*'`, `~/.cache/workday/`.
Tests: `python3 -m unittest discover -s tests`.

## Safety rules

- Backup uses `rclone copy` (never `sync`), so nothing on Drive is deleted. A local item is removed only
  when `rclone check --one-way` matches every file in it. Every run writes a manifest, locally and to `_manifests/`.
- `gsync` refuses files that look like secrets and aborts a failed rebase. Nothing is force-pushed.
- APK installs keep app data (`-r`). Downgrades need `day apk --downgrade`. A signature mismatch is reported,
  never fixed by uninstalling.
- The TOTP secret is never stored. Cancelling the code popup stops the VPN.
