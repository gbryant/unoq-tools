#!/usr/bin/env python3
"""bt.py — connect / pair / report Bluetooth audio on the Arduino Uno Q, from your host over adb.

Targets the board's BlueZ over `bluetoothctl` (no sudo — BT ops are user-session). Most useful
daily: reconnect a speaker that idle-disconnected. Also drives the one-time pairing flow that
setup-bt-audio.py calls for its last step.

  bt.py                 status: paired devices, which are connected, the default sink
  bt.py connect [MAC]   connect (no MAC → the lone paired device, else pick) + make it default
  bt.py disconnect [MAC]
  bt.py pair            scan, pick a new speaker, pair+trust+connect, set default + test

Needs the board reachable over adb and `bluetooth.service` running (setup-bt-audio.py enables it).
"""
import shlex
import subprocess
import sys
import time

UENV = 'XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus'


# ── board I/O (timeout-bounded — bluetoothctl hangs if the daemon is down) ───
def usr(cmd, timeout=20):
    try:
        p = subprocess.run(["adb", "shell", f"{UENV} {cmd}"],
                          capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "(timed out)"
    return (p.stdout + p.stderr).strip()


def have_board():
    d = subprocess.run(["adb", "devices"], capture_output=True, text=True)
    return any(l.strip().endswith("device") for l in d.stdout.splitlines()[1:])


def ask(prompt, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        a = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default
    return default if not a else a.startswith("y")


# ── device queries ───────────────────────────────────────────────────────────
def _devices(which):
    """which in {'Paired','Connected',''}; returns [(mac, name), ...]."""
    out = usr(f"bluetoothctl devices {which}".strip())
    devs = []
    for line in out.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) >= 2 and parts[0] == "Device":
            devs.append((parts[1], parts[2] if len(parts) > 2 else parts[1]))
    return devs


def sink_for(mac):
    return f"bluez_output.{mac.replace(':', '_')}.1"


def default_sink():
    return usr("pactl get-default-sink") or "(unknown)"


# ── operations ───────────────────────────────────────────────────────────────
def _pick(devs, what="device"):
    if not devs:
        return None
    if len(devs) == 1:
        return devs[0][0]
    print(f"  multiple {what}s:")
    for i, (m, n) in enumerate(devs):
        print(f"    {i}) {n} [{m}]")
    try:
        return devs[int(input("  pick #: ").strip())][0]
    except (ValueError, IndexError, EOFError):
        return None


def make_default(mac, announce=False):
    """Wait for the speaker's PipeWire sink to appear after connect, set it default, test."""
    sink = sink_for(mac)
    for _ in range(10):
        if sink in usr("pactl list sinks short"):
            break
        time.sleep(0.5)
    else:
        print(f"  sink {sink} never appeared — is A2DP up? (setup-bt-audio.py seat fix)")
        return
    usr(f"pactl set-default-sink {sink}")
    print(f"  default sink → {sink}")
    if announce:
        usr('espeak-ng "connected"')
        print("  (sent a test tone; first sound after idle can be swallowed — repeat if silent)")


def cmd_status():
    conn = {m for m, _ in _devices("Connected")}
    paired = _devices("Paired")
    if not paired:
        print("no paired devices — run `bt.py pair`")
    else:
        print("paired:")
        for m, n in paired:
            print(f"  {'● connected' if m in conn else '○ disconnected'}  {n} [{m}]")
    print(f"default sink: {default_sink()}")


def cmd_connect(mac=None):
    if not mac:
        mac = _pick(_devices("Paired"), "paired device")
        if not mac:
            print("nothing to connect (no paired device / no pick) — try `bt.py pair`")
            return
    out = usr(f"bluetoothctl connect {shlex.quote(mac)}", timeout=25)
    if "successful" in out.lower():
        print(f"  connected {mac} ✓")
        make_default(mac)
    else:
        print(f"  connect failed: {out.splitlines()[-1] if out else '?'}")


def cmd_disconnect(mac=None):
    if not mac:
        mac = _pick(_devices("Connected"), "connected device")
        if not mac:
            print("nothing connected")
            return
    out = usr(f"bluetoothctl disconnect {shlex.quote(mac)}", timeout=20)
    print(f"  disconnected {mac} ✓" if "successful" in out.lower() else f"  {out.splitlines()[-1] if out else '?'}")


def cmd_pair():
    print("  put the speaker in pairing mode, then scanning 15 s...")
    usr("bluetoothctl power on")
    usr("bluetoothctl --timeout 15 scan on", timeout=30)
    devs = _devices("")            # all discovered
    if devs:
        print("  discovered:")
        for m, n in devs:
            print(f"    {n} [{m}]")
    mac = input("  speaker MAC (AA:BB:CC:DD:EE:FF): ").strip()
    if not mac:
        return
    for action in ("pair", "trust", "connect"):
        out = usr(f"bluetoothctl {action} {shlex.quote(mac)}", timeout=25)
        ok = action == "trust" or "successful" in out.lower()
        print(f"  {action:<8} {'✓' if ok else (out.splitlines()[-1] if out else '?')}")
    make_default(mac, announce=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return
    if not have_board():
        sys.exit("no adb device — plug the Uno Q in over USB")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "status":
        cmd_status()
    elif cmd == "connect":
        cmd_connect(arg)
    elif cmd == "disconnect":
        cmd_disconnect(arg)
    elif cmd == "pair":
        cmd_pair()
    else:
        sys.exit(f"unknown command '{cmd}' — see `bt.py --help`")


if __name__ == "__main__":
    main()
