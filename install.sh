#!/bin/bash
# User part of the workday automation (no sudo). Safe to re-run.
#   ~/Workspace/workday-automation/install.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"; UNITS="$HOME/.config/systemd/user"; APPS="$HOME/.local/share/applications"
mkdir -p "$BIN" "$UNITS" "$APPS" "$HOME/.config/workday"

echo "--- commands: day, gst, gsync -> $BIN"
for cmd in day gst gsync; do ln -sfn "$HERE/bin/$cmd" "$BIN/$cmd"; done

[ -f "$HOME/.config/workday/config.toml" ] || cp "$HERE/config.example.toml" "$HOME/.config/workday/config.toml"

echo "--- APK baseline (existing downloads are not installed)"
python3 "$HERE/bin/day" apk --quiet

echo "--- systemd user units"
cp "$HERE"/systemd/workday-* "$UNITS/"
systemctl --user daemon-reload
systemctl --user enable --now workday-vpn-agent.service workday-recorder.service workday-apk.path \
    workday-watch.timer workday-report.timer workday-backup.timer

echo "--- pre-push test gate"
for repo in "$HOME/Workspace/greybox-mobithon-2026"; do
    hook="$repo/.git/hooks/pre-push"
    if [ -e "$hook" ] && ! grep -q workday-automation "$hook"; then mv "$hook" "$hook.before-workday"; fi
    install -m 0755 "$HERE/hooks/pre-push" "$hook"
    echo "    $hook"
done

echo "--- launchers (Activities: Day Start / Day End)"
for d in "$HERE"/desktop/*.desktop; do sed "s|@HOME@|$HOME|g" "$d" > "$APPS/$(basename "$d")"; done

echo
echo "Done. Next, once:"
echo "  sudo $HERE/install-root.sh      # bizom-env, VPN service, sudoers"
echo "  day vpn setup                   # VPN username + password into the keyring"
echo "  day backup setup                # rclone + Google Drive remote (browser sign-in)"
