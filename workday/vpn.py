"""VPN: the user-side agent for bizom-vpn.service's management socket, plus up / down / status.

OpenVPN (root, systemd) holds at start and asks the management client for credentials.
The agent answers with the username / password from the GNOME keyring (secret-tool) and the
authenticator code typed into a popup - the static-challenge reply
`SCRV1:<base64 password>:<base64 code>`. The TOTP secret is never stored. On a drop OpenVPN
reconnects by itself; when the server wants a fresh code the popup comes back
(--auth-retry interact). Cancelling the popup stops the VPN.
"""

from __future__ import annotations

import base64
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from workday.common import load_state, notify, run, save_state
from workday.config import CONFIG, HOME

SECRET_SERVICE = "bizom-vpn"
FALLBACK_CRED = HOME / ".config" / "workday" / "vpn-credentials"
CODE_RE = re.compile(r"^\d{6,8}$")


# --- credentials ---------------------------------------------------------------------------

def _secret(field: str) -> str:
    if shutil.which("secret-tool"):
        out = run(["secret-tool", "lookup", "service", SECRET_SERVICE, "field", field], timeout=15)
        if out.returncode == 0 and out.stdout:
            return out.stdout.rstrip("\n")
    if FALLBACK_CRED.exists():
        lines = FALLBACK_CRED.read_text().splitlines()
        return {"username": lines[0] if lines else "", "password": lines[1] if len(lines) > 1 else ""}[field]
    return ""


def credentials() -> tuple[str, str]:
    return _secret("username"), _secret("password")


def setup_credentials() -> int:
    """Ask username + password in a dialog and keep them in the keyring (or a 0600 file)."""
    out = run(["zenity", "--forms", "--title=Bizom VPN", "--text=VPN login (stored in your keyring)",
               "--add-entry=Username", "--add-password=Password", "--separator=\x1f"], timeout=300)
    if out.returncode != 0 or "\x1f" not in out.stdout:
        print("cancelled")
        return 1
    username, password = out.stdout.rstrip("\n").split("\x1f", 1)
    if shutil.which("secret-tool"):
        for field, value in (("username", username), ("password", password)):
            res = subprocess.run(["secret-tool", "store", f"--label=Bizom VPN {field}", "service",
                                  SECRET_SERVICE, "field", field], input=value, text=True, capture_output=True)
            if res.returncode != 0:
                print(f"keyring store failed: {res.stderr.strip()}")
                return 1
        print("VPN credentials saved in the GNOME keyring.")
    else:
        FALLBACK_CRED.parent.mkdir(parents=True, exist_ok=True)
        FALLBACK_CRED.touch(mode=0o600)
        FALLBACK_CRED.chmod(0o600)
        FALLBACK_CRED.write_text(f"{username}\n{password}\n")
        print(f"secret-tool missing - saved to {FALLBACK_CRED} (0600). Run install-root.sh to use the keyring.")
    return 0


def ask_code(retry: bool = False) -> str | None:
    """The authenticator code from a popup; None when cancelled or timed out."""
    text = ("Wrong or expired code - enter a new authenticator code" if retry
            else "Enter your authenticator code for the Bizom VPN")
    for _ in range(3):
        out = run(["zenity", "--entry", "--title=Bizom VPN", f"--text={text}", "--timeout=180",
                   "--width=360"], timeout=200)
        if out.returncode != 0:
            return None
        code = out.stdout.strip().replace(" ", "")
        if CODE_RE.match(code):
            return code
        text = "That is not a 6-digit code - try again"
    return None


# --- management protocol -------------------------------------------------------------------

def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def challenge_reply(password: str, code: str) -> str:
    b64 = lambda s: base64.b64encode(s.encode()).decode()  # noqa: E731
    return f"SCRV1:{b64(password)}:{b64(code)}"


def parse_state(line: str) -> dict | None:
    """`>STATE:<time>,<state>,<detail>,<local ip>,...` -> {state, detail, ip}."""
    if not line.startswith(">STATE:"):
        return None
    parts = line[len(">STATE:"):].split(",")
    if len(parts) < 2:
        return None
    return {"state": parts[1], "detail": parts[2] if len(parts) > 2 else "",
            "ip": parts[3] if len(parts) > 3 else "", "at": time.strftime("%Y-%m-%d %H:%M:%S")}


class Agent:
    def __init__(self, sock_path: str):
        self.sock_path = sock_path
        self.sock: socket.socket | None = None
        self.failed_once = False
        self.last_state = ""

    def send(self, line: str) -> None:
        assert self.sock is not None
        self.sock.sendall((line + "\n").encode())

    def handle(self, line: str) -> None:
        if line.startswith(">HOLD:"):
            self.send("hold release")
        elif line.startswith(">PASSWORD:Verification Failed"):
            self.failed_once = True
            notify("Bizom VPN", "Login rejected - wrong or expired code.", "critical")
        elif line.startswith(">PASSWORD:Need 'Auth'"):
            self.answer_auth(static_challenge="SC:" in line)
        elif line.startswith(">PASSWORD:Need"):
            notify("Bizom VPN", f"OpenVPN asks for something the agent cannot answer: {line[10:]}", "critical")
        else:
            state = parse_state(line)
            if state:
                self.on_state(state)

    def answer_auth(self, static_challenge: bool) -> None:
        username, password = credentials()
        if not username or not password:
            notify("Bizom VPN", "No VPN credentials saved - run `day vpn setup`. Stopping the VPN.", "critical")
            self.send("signal SIGTERM")
            return
        if static_challenge:
            code = ask_code(retry=self.failed_once)
            if code is None:
                notify("Bizom VPN", "Code popup cancelled - VPN stopped. `day vpn up` to try again.")
                self.send("signal SIGTERM")
                return
            password = challenge_reply(password, code)
        self.send(f"username \"Auth\" {quote(username)}")
        self.send(f"password \"Auth\" {quote(password)}")

    def on_state(self, state: dict) -> None:
        save_state("vpn", state)
        name = state["state"]
        if name == self.last_state:
            return
        if name == "CONNECTED":
            self.failed_once = False
            notify("Bizom VPN", f"Connected ({state['ip']})")
        elif name == "RECONNECTING" and self.last_state == "CONNECTED":
            notify("Bizom VPN", f"Connection dropped ({state['detail']}) - reconnecting…", "critical")
        self.last_state = name

    def serve_once(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.sock_path)
        self.send("state on")
        buffer = b""
        while True:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                self.handle(raw.decode(errors="replace").rstrip("\r"))
        self.sock.close()
        self.sock = None


def agent_main() -> int:
    agent = Agent(CONFIG["vpn"]["socket"])
    was_up = False
    while True:
        try:
            agent.serve_once()
            was_up = True
        except (FileNotFoundError, ConnectionRefusedError, PermissionError):
            pass
        except OSError as exc:
            print(f"management socket error: {exc}", file=sys.stderr, flush=True)
        if was_up:
            # OpenVPN went away (stopped or crashed); systemd restarts it on failure.
            save_state("vpn", {"state": "DOWN", "detail": "", "ip": "", "at": time.strftime("%Y-%m-%d %H:%M:%S")})
            if agent.last_state == "CONNECTED":
                notify("Bizom VPN", "VPN process stopped.")
            agent.last_state = ""
            was_up = False
        time.sleep(2)


# --- control -------------------------------------------------------------------------------

def _unit_installed() -> bool:
    return Path("/etc/systemd/system", CONFIG["vpn"]["unit"]).exists()


def manual_openvpn_running() -> bool:
    """openvpn started by con.sh (not by the unit)."""
    unit_active = run(["systemctl", "is-active", CONFIG["vpn"]["unit"]]).stdout.strip() == "active"
    return not unit_active and run(["pgrep", "-x", "openvpn"]).returncode == 0


def is_connected() -> bool:
    if manual_openvpn_running():
        return run(["ip", "-brief", "link", "show", "type", "tun"]).stdout.strip() != ""
    return load_state("vpn").get("state") == "CONNECTED" and \
        run(["systemctl", "is-active", CONFIG["vpn"]["unit"]]).stdout.strip() == "active"


def up(wait: bool = True) -> tuple[bool, str]:
    if is_connected():
        return True, "already connected"
    if manual_openvpn_running():
        return False, "an openvpn from con.sh is running but not connected - stop it first"
    if not _unit_installed():
        return False, "bizom-vpn.service not installed - run `sudo install-root.sh` (meanwhile: ./con.sh)"
    if not credentials()[0]:
        return False, "no saved VPN credentials - run `day vpn setup`"
    run(["systemctl", "--user", "start", "workday-vpn-agent.service"])
    res = run(["sudo", "-n", "systemctl", "start", CONFIG["vpn"]["unit"]])
    if res.returncode != 0:
        return False, f"could not start the VPN unit: {res.stderr.strip()}"
    if not wait:
        return True, "starting (enter the code in the popup)"
    deadline = time.time() + CONFIG["vpn"]["connect_timeout"]
    while time.time() < deadline:
        if is_connected():
            return True, f"connected {load_state('vpn').get('ip', '')}"
        if run(["systemctl", "is-active", CONFIG["vpn"]["unit"]]).stdout.strip() in {"inactive", "failed"}:
            return False, "VPN stopped (popup cancelled or login failed)"
        time.sleep(2)
    return False, "not connected yet - still waiting for the code?"


def down() -> tuple[bool, str]:
    if manual_openvpn_running():
        return False, "openvpn was started by con.sh - stop it with Ctrl+C in that terminal"
    res = run(["sudo", "-n", "systemctl", "stop", CONFIG["vpn"]["unit"]])
    return res.returncode == 0, (res.stderr.strip() or "stopped")


def status_line() -> str:
    state = load_state("vpn")
    if manual_openvpn_running():
        return "openvpn via con.sh " + ("(tun up)" if is_connected() else "(no tun)")
    return f"{state.get('state', 'unknown')} {state.get('ip', '')} since {state.get('at', '?')}".strip()


if __name__ == "__main__":
    sys.exit(agent_main())
