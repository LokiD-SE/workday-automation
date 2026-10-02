#!/bin/bash
# One-time root part of the workday automation. Run from a terminal:
#   sudo ~/Workspace/workday-automation/install-root.sh
# Installs: bizom-env helper, bizom-vpn.service (+ the .ovpn, root-only), a narrow sudoers
# rule, and the packages the user side needs (libsecret-tools, wl-clipboard).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
USER_NAME="${SUDO_USER:?run with sudo from your own account}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "--- packages"
apt-get install -y libsecret-tools wl-clipboard >/dev/null

echo "--- /usr/local/sbin/bizom-env"
install -m 0755 -o root -g root "$HERE/root/bizom-env" /usr/local/sbin/bizom-env

echo "--- VPN config + bizom-vpn.service"
install -d -m 0755 /etc/openvpn/client
install -m 0600 -o root -g root "$USER_HOME/client.ovpn" /etc/openvpn/client/bizom.ovpn
sed "s/@USER@/$USER_NAME/g" "$HERE/root/bizom-vpn.service" > /etc/systemd/system/bizom-vpn.service
chmod 0644 /etc/systemd/system/bizom-vpn.service
systemctl daemon-reload

echo "--- sudoers"
TMP="$(mktemp)"
sed "s/@USER@/$USER_NAME/g" "$HERE/root/sudoers.template" > "$TMP"
visudo -cf "$TMP" >/dev/null
install -m 0440 -o root -g root "$TMP" /etc/sudoers.d/workday-automation
rm -f "$TMP"

echo "Done. bizom-env, bizom-vpn.service and the sudoers rule are installed."
echo "The VPN is not enabled at boot - 'day start' brings it up after login."
