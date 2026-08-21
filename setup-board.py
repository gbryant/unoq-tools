#!/usr/bin/env python3
"""setup-board.py — interactive wizard to set up an Arduino Uno Q from your host, over adb.

Just run it (no arguments). It inspects the board and walks each setup step — expired-password
reset, hostname, ssh, mDNS, Wi-Fi, and the headless slim — showing the CURRENT state and asking
permission before it changes anything. Idempotent: whatever is already done is reported with a ✓
and skipped, so it's safe to re-run.

A fresh stock image lands you as the `arduino` user over adb with ssh/avahi DISABLED, no Wi-Fi,
and the account having NO password but already EXPIRED (sudo blocked until one is set).
adb-over-USB is the only door until this runs. There is no stock password to look up: `passwd`
asks for the current one and you press Enter (the image ships an empty hash in /etc/shadow).
See the commander repo’s docs/getting-started-unoq.md for the full Uno Q track.
"""
import getpass
import shlex
import subprocess
import sys


# ── board I/O ────────────────────────────────────────────────────────────────
def adb(args, **kw):
    return subprocess.run(["adb", *args], text=True, capture_output=True, **kw)


def sh(cmd):
    """Run a plain (non-sudo) command on the board; return trimmed stdout."""
    return adb(["shell", cmd]).stdout.strip()


_PW = {"v": None}


def sudo(cmd):
    """Run a command as root on the board, feeding the cached sudo password via stdin
    (prompted once, never placed in argv). Returns (returncode, combined-output)."""
    if _PW["v"] is None:
        _PW["v"] = getpass.getpass("  arduino sudo password: ")
    p = subprocess.run(["adb", "shell", f"sudo -S -p '' {cmd}"],
                       input=_PW["v"] + "\n", text=True, capture_output=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def ask(prompt, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        a = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default
    return default if not a else a.startswith("y")


# ── state detection (no sudo needed) ─────────────────────────────────────────
def password_expired():
    # `chage -l` works on your own account without sudo; an expired password reads
    # "password must be changed" instead of a date.
    return "must be changed" in sh("chage -l arduino 2>/dev/null")


def svc_ok(unit):
    return (sh(f"systemctl is-active {unit}") == "active" and
            sh(f"systemctl is-enabled {unit}") == "enabled")


def wifi_status():
    line = sh("nmcli -t -f DEVICE,STATE,CONNECTION device status | grep '^wlan0:'")
    parts = line.split(":")
    state = parts[1] if len(parts) > 1 else "unknown"
    conn = parts[2] if len(parts) > 2 else ""
    return state, conn


def slim_status():
    return {
        "default-target":  sh("systemctl get-default"),
        "lightdm":         sh("systemctl is-active lightdm"),
        "ModemManager":    sh("systemctl is-active ModemManager"),
        "bluetooth":       sh("systemctl is-active bluetooth"),
        "docker":          sh("systemctl is-enabled docker"),
        "arduino-app-cli": sh("systemctl is-enabled arduino-app-cli"),
    }


def is_slimmed(s):
    return (s["default-target"] == "multi-user.target" and
            s["lightdm"] != "active" and s["ModemManager"] != "active" and
            s["bluetooth"] != "active" and s["docker"] == "masked" and
            s["arduino-app-cli"] == "masked")


_SLIM_SH = r"""
set -e
systemctl set-default multi-user.target
systemctl disable --now lightdm.service || true
systemctl disable --now ModemManager.service || true
systemctl disable --now bluetooth.service || true
systemctl mask --now docker.service docker.socket containerd.service
APPCLI=/etc/systemd/system/arduino-app-cli.service
if [ -f "$APPCLI" ] && [ ! -L "$APPCLI" ]; then
  systemctl stop arduino-app-cli.service || true
  mv "$APPCLI" "$APPCLI.commander-bak"
  ln -sf /dev/null "$APPCLI"
  systemctl daemon-reload
fi
"""


# ── steps (each: report state, then act only if needed + permitted) ──────────
def step_password():
    if not password_expired():
        print("password   : not expired ✓")
        return True
    print("password   : EXPIRED — sudo is blocked until it's reset (stock first-boot state)")
    if not ask("  reset it now?"):
        print("  (skipped — nothing needing sudo can run until this is done)")
        return False
    print("  passwd asks for the current password first — on a stock image it is EMPTY,")
    print("  so just press Enter, then type the new password twice:")
    subprocess.run(["adb", "shell", "-t", "passwd"])
    if password_expired():
        print("  still expired — reset didn't complete (try again, or `adb shell -t passwd`)")
        return False
    print("  reset ✓  (you'll enter the new password once more for sudo)")
    return True


def step_hostname():
    print(f"hostname   : {sh('hostname')}")
    if not ask("  change it?", default=False):
        return
    new = input("  new hostname: ").strip()
    if not new:
        return
    rc, err = sudo(f"hostnamectl set-hostname {shlex.quote(new)}")
    print("  set ✓" if not rc else f"  failed: {err}")


def step_service(unit, label):
    if svc_ok(unit):
        print(f"{label:<11}: enabled ✓")
        return
    print(f"{label:<11}: {sh(f'systemctl is-active {unit}')} / {sh(f'systemctl is-enabled {unit}')}")
    if not ask(f"  enable {unit}?"):
        return
    rc, err = sudo(f"systemctl enable --now {unit}")
    print("  enabled ✓" if not rc else f"  failed: {err}")


def step_ssh():
    if svc_ok("ssh"):
        print("ssh        : enabled ✓")
        return
    print(f"ssh        : {sh('systemctl is-active ssh')} / {sh('systemctl is-enabled ssh')}")
    if not ask("  enable ssh?"):
        return
    # The stock image ships with NO sshd host keys (sshd: "no hostkeys available -- exiting"),
    # so generate them first; reset-failed clears any prior rate-limited start.
    if sh("ls /etc/ssh/ssh_host_*key 2>/dev/null") == "":
        print("  no sshd host keys — generating (ssh-keygen -A)")
        rc, err = sudo("ssh-keygen -A")
        if rc:
            print(f"  host-key gen failed: {err}")
            return
    rc, err = sudo("sh -c 'systemctl reset-failed ssh.service 2>/dev/null; "
                  "systemctl enable --now ssh'")
    print("  enabled ✓" if not rc and svc_ok("ssh") else f"  failed: {err}")


def step_wifi():
    state, conn = wifi_status()
    if state == "connected":
        print(f"wifi       : connected ({conn}) ✓")
        if not ask("  join a different network?", default=False):
            return
    else:
        print(f"wifi       : {state or 'unknown'}")
        if not ask("  join a Wi-Fi network?"):
            return
    ssid = input("  SSID: ").strip()
    if not ssid:
        return
    psk = getpass.getpass("  Wi-Fi password: ")
    rc, err = sudo(f"nmcli device wifi connect {shlex.quote(ssid)} password {shlex.quote(psk)}")
    print("  connected ✓" if not rc else f"  failed: {err}")


def step_slim():
    s = slim_status()
    if is_slimmed(s):
        print("slim       : already a headless host ✓")
        return
    print("slim       : not trimmed (" + ", ".join(f"{k}={v}" for k, v in s.items()) + ")")
    if not ask("  slim to a headless host (drop GUI + ModemManager/bluetooth + App Lab/Docker)?",
               default=False):
        return
    rc, err = sudo("sh -c " + shlex.quote(_SLIM_SH))
    print("  slimmed ✓" if not rc else f"  failed: {err}")


def main():
    devs = [l for l in adb(["devices"]).stdout.splitlines()[1:] if l.strip().endswith("device")]
    if not devs:
        sys.exit("no adb device — plug the Uno Q in over USB (adb devices shows nothing)")

    print(f"board      : {sh('hostname')}  ({sh('. /etc/os-release 2>/dev/null; echo $PRETTY_NAME')})")
    print("Each step shows the current state and asks before changing anything.\n")

    if not step_password():
        return                       # can't do anything needing sudo until this clears
    step_hostname()
    step_ssh()
    step_service("avahi-daemon", "mDNS")
    step_wifi()
    step_slim()

    if ask("\nreboot now to apply everything cleanly?", default=False):
        sudo("reboot")
        print("rebooting…")


if __name__ == "__main__":
    main()
