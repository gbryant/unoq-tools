#!/usr/bin/env python3
"""setup-bt-audio.py — interactive wizard for headless Bluetooth audio on an Arduino Uno Q,
driven from your host over adb. Scripts docs/unoq-bluetooth-audio.md.

Just run it (no arguments). It inspects each step, shows the current state, and asks permission
before changing anything — idempotent, safe to re-run (done steps show ✓ and are skipped).

The board ships PipeWire + WirePlumber. The non-obvious headless gotcha this automates: on a
board reached over adb/ssh (no active logind seat) WirePlumber won't start its bluez monitor, so
a speaker "connects" but plays nothing — fixed by a seat-monitoring override (step 4). Pairing
(step 7) is interactive: you put the speaker in pairing mode and pick it.

Run setup-board.py first (this assumes the password is reset so sudo works).
"""
import base64
import getpass
import subprocess
import sys

import bt   # sibling tool in this repo — the pair/connect flow lives here

USER = "arduino"
# User-session (PipeWire/BlueZ) commands need these over a non-login adb shell — .bashrc isn't
# sourced by `adb shell <cmd>`, so we always prefix them explicitly.
UENV = 'XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus'
SEAT_DIR = "$HOME/.config/wireplumber/wireplumber.conf.d"
SEAT_CONF = SEAT_DIR + "/51-bluez-no-seat.conf"
SEAT_CONF_BODY = (
    "# Headless board (linger, no active logind seat): don't gate the bluez monitor on an\n"
    "# active seat, or WirePlumber never starts it and Bluetooth has no A2DP endpoint.\n"
    "wireplumber.profiles = {\n"
    "  main = {\n"
    "    monitor.bluez.seat-monitoring = disabled\n"
    "  }\n"
    "}\n"
)


# ── board I/O ────────────────────────────────────────────────────────────────
# Every call is bounded by a timeout — bluetoothctl in particular hangs forever over a
# non-interactive adb shell if the bluez daemon is down, so nothing may block unbounded.
def _run(args, inp=None, timeout=20):
    try:
        return subprocess.run(args, text=True, capture_output=True, input=inp, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def sh(cmd, timeout=20):
    """Plain (non-sudo) command on the board; trimmed stdout ('' on timeout)."""
    p = _run(["adb", "shell", cmd], timeout=timeout)
    return p.stdout.strip() if p else ""


def usr(cmd, timeout=20):
    """A user-session command (PipeWire/BlueZ) with the env prefix; trimmed output."""
    p = _run(["adb", "shell", f"{UENV} {cmd}"], timeout=timeout)
    return (p.stdout + p.stderr).strip() if p else "(timed out)"


_PW = {"v": None}


def sudo(cmd, timeout=300):
    """Run as root, feeding the cached sudo password via stdin. Returns (rc, output).
    Generous default timeout so apt installs aren't cut off."""
    if _PW["v"] is None:
        _PW["v"] = getpass.getpass(f"  {USER} sudo password: ")
    p = _run(["adb", "shell", f"sudo -S -p '' {cmd}"], inp=_PW["v"] + "\n", timeout=timeout)
    if p is None:
        return 124, "(timed out)"
    return p.returncode, (p.stdout + p.stderr).strip()


def ask(prompt, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        a = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default
    return default if not a else a.startswith("y")


# ── steps ────────────────────────────────────────────────────────────────────
# pulseaudio-utils provides `pactl` (the pipewire-pulse compat CLI the guide uses); it is NOT
# classic pulseaudio (that would fight PipeWire). espeak-ng is the quick audio test.
PKGS = ["bluez", "libspa-0.2-bluetooth", "pulseaudio-utils", "espeak-ng"]


def step_packages():
    missing = [p for p in PKGS
               if "install ok installed" not in sh(f"dpkg-query -W -f='${{Status}}' {p} 2>/dev/null")]
    if not missing:
        print("packages   : bluez + libspa-0.2-bluetooth + espeak-ng ✓")
        return
    print(f"packages   : missing {', '.join(missing)}")
    if not ask("  apt install them?"):
        return
    rc, err = sudo(f"sh -c 'apt-get update && apt-get install -y {' '.join(missing)}'")
    print("  installed ✓" if not rc else f"  failed: {err.splitlines()[-1] if err else '?'}")


def step_groups():
    groups = sh(f"id -nG {USER}").split()
    need = [g for g in ("bluetooth", "audio") if g not in groups]
    if not need:
        print("groups     : bluetooth + audio ✓")
        return
    print(f"groups     : {USER} not in {', '.join(need)}")
    if not ask(f"  add {USER} to bluetooth,audio?"):
        return
    rc, err = sudo(f"usermod -aG bluetooth,audio {USER}")
    print("  added ✓ (takes effect in new shells)" if not rc else f"  failed: {err}")


def step_linger():
    if sh(f"loginctl show-user {USER} -p Linger 2>/dev/null") == "Linger=yes":
        print("linger     : enabled ✓")
        return
    print("linger     : off — user services won't run without a login")
    if not ask("  enable linger?"):
        return
    rc, err = sudo(f"loginctl enable-linger {USER}")
    print("  enabled ✓" if not rc else f"  failed: {err}")


def step_bt_service():
    if sh("systemctl is-active bluetooth") == "active" and \
       sh("systemctl is-enabled bluetooth") == "enabled":
        print("bluetooth  : service active ✓")
        return
    state = sh("systemctl is-active bluetooth")
    print(f"bluetooth  : service {state} — BT audio needs it (slim.sh disables host BT as unused)")
    if not ask("  enable bluetooth.service?"):
        return
    rc, err = sudo("systemctl enable --now bluetooth")
    print("  enabled ✓" if not rc else f"  failed: {err}")


def step_bashrc():
    if "XDG_RUNTIME_DIR" in sh("grep XDG_RUNTIME_DIR ~/.bashrc 2>/dev/null"):
        print("bashrc env : present ✓")
        return
    print("bashrc env : XDG_RUNTIME_DIR / DBUS not in ~/.bashrc (audio tools fail in a bare shell)")
    if not ask("  add them to ~/.bashrc?"):
        return
    sh("sh -c 'printf \"%s\\n%s\\n\" "
       "\"export XDG_RUNTIME_DIR=/run/user/\\$(id -u)\" "
       "\"export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/\\$(id -u)/bus\" >> ~/.bashrc'")
    print("  added ✓")


def seat_conf_present():
    return sh(f'test -f {SEAT_CONF} && echo yes') == "yes"


def step_seat():
    if seat_conf_present():
        print("seat fix   : present ✓")
    else:
        print("seat fix   : MISSING — the headless killer (no A2DP without it)")
        if not ask("  write the seat-monitoring override?"):
            return
        # base64 the body so no shell quoting/tilde issues; decode on the board.
        b64 = base64.b64encode(SEAT_CONF_BODY.encode()).decode()
        sh(f"mkdir -p {SEAT_DIR}")
        sh(f"sh -c 'echo {b64} | base64 -d > {SEAT_CONF}'")
        print("  written ✓" if seat_conf_present() else "  write FAILED")
    # restart wireplumber so the override takes effect, then check A2DP is registered
    if ask("  restart wireplumber and check A2DP now?"):
        usr("systemctl --user restart wireplumber")
        audio = usr("bluetoothctl show | grep -iE 'Audio Source|Audio Sink'")
        print("  A2DP registered ✓" if "Audio" in audio
              else "  A2DP still absent — check the override + WirePlumber log")


def step_pair():
    if not ask("\npair a speaker now?", default=False):
        return
    bt.cmd_pair()      # the pairing flow lives in bt.py (also a standalone tool)


def main():
    if not bt.have_board():
        sys.exit("no adb device — plug the Uno Q in over USB")
    print(f"board      : {sh('hostname')}")
    # wpctl is always present on a PipeWire image; pactl arrives with pulseaudio-utils below.
    server = usr("wpctl status 2>/dev/null | head -1") or "(PipeWire not reachable)"
    print(f"audio      : {server}")
    print("Each step shows the current state and asks before changing anything.\n")

    step_packages()
    step_groups()
    step_linger()
    step_bt_service()   # must precede seat/pair — bluetoothctl hangs if the daemon is down
    step_bashrc()
    step_seat()
    step_pair()
    print("\ndone. Daily: `bt.py connect` to reconnect the speaker; `volume.py` to adjust volume.")


if __name__ == "__main__":
    main()
