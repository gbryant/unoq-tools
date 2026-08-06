#!/usr/bin/env python3
"""bt.py — connect / pair / report Bluetooth audio on the Arduino Uno Q, from your host over adb.

Targets the board's BlueZ over `bluetoothctl` (no sudo — BT ops are user-session). Most useful
daily: reconnect a speaker that idle-disconnected. Also drives the one-time pairing flow that
setup-bt-audio.py calls for its last step.

  bt.py                 status: paired devices, which are connected, the default sink
  bt.py connect [MAC]   connect (no MAC → the lone paired device, else pick) + make it default
  bt.py disconnect [MAC]
  bt.py pair            scan, pick a new speaker, pair+trust+connect, set default + test
  bt.py pair --diff     two scans (speaker off, then on) — pick from ONLY what appeared
  bt.py forget [MAC]    disconnect + remove a pairing, so it can't auto-reconnect

`pair --diff` is for the speaker whose name you can't recognise — a brandless one advertising
a bare MAC, or a crowded RF neighbourhood. It scans once with the speaker off to take a
baseline, once with it on, and offers the difference. Nothing to identify by eye.

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
def _pick(devs, what="device", confirm=False):
    """confirm=True: show a lone candidate and ask, instead of selecting it silently —
    worth it when the action is destructive-ish (pairing) rather than routine."""
    if not devs:
        return None
    if len(devs) == 1:
        mac, name = devs[0]
        if confirm:
            print(f"  found: {name} [{mac}]")
            return mac if ask("  pair with this?") else None
        return mac
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
    if not bt_ready():
        return
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
    if not bt_ready():
        return
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


SCAN_SECS = 15


def adapter_state():
    """(present, powered) from `bluetoothctl show`."""
    out = usr("bluetoothctl show", timeout=15)
    if not out or "No default controller" in out:
        return False, False
    return True, "Powered: yes" in out


def bt_ready():
    """Everything below the pairing flow that has to be true first. Worth checking
    explicitly: when the controller is down, BlueZ doesn't error usefully — it reports an
    EMPTY paired list (so paired speakers look unpaired and re-pairing seems reasonable)
    and then fails with AuthenticationFailed. The real cause is invisible without dmesg."""
    if usr("systemctl is-active bluetooth", timeout=10) != "active":
        print("  bluetooth.service isn't active — BT commands would just time out.\n"
              "  Fix: run setup-bt-audio.py (it enables the service), or\n"
              "       adb shell sudo systemctl enable --now bluetooth")
        return False

    present, powered = adapter_state()
    if not present:
        print("  no Bluetooth controller found (`bluetoothctl show` has no adapter).")
        return False
    if not powered:
        usr("bluetoothctl power on", timeout=15)      # normal case: just powered down
        _, powered = adapter_state()
    if not powered:
        print("  the Bluetooth controller is DOWN and refuses to power on.\n"
              "  Most likely the QCA SoC crashed — a stalled speaker link can take it out,\n"
              "  and its firmware-reload recovery sometimes times out too. Confirm with:\n"
              '    adb shell "dmesg | grep -i hci | tail"\n'
              "  ('crash the soc' / 'Change address cmd failed' / bluetoothd 'Invalid Index').\n"
              "  Recovery is a board reboot — power-cycling the UART-attached chip:\n"
              "    adb reboot\n"
              "  NOTE: while it's down BlueZ reports NO paired devices, so anything you had\n"
              "  paired will look unpaired. Don't re-pair; reboot first.")
        return False
    return True


def _scan(secs=SCAN_SECS):
    """Discovery pass; returns [(mac, name), ...] known to BlueZ afterwards."""
    usr("bluetoothctl power on")
    usr(f"bluetoothctl --timeout {secs} scan on", timeout=secs + 15)
    return _devices("")


def _candidates_plain(paired):
    print(f"  put the speaker in pairing mode, then scanning {SCAN_SECS} s...")
    return [(m, n) for m, n in _scan() if m not in paired]


def _candidates_diff(paired):
    """Two scans bracketing the speaker being switched on; the new speaker is the set
    difference. Identifies a device you can't recognise by name."""
    input(f"  1/2 — make sure the speaker is OFF, then press Enter (scans {SCAN_SECS} s)...")
    before = {m for m, _ in _scan()}
    print(f"       baseline: {len(before)} device(s) in range")
    input("  2/2 — now switch the speaker ON / into pairing mode, then press Enter...")
    after = _scan()
    new = [(m, n) for m, n in after if m not in before and m not in paired]
    if new:
        return new
    print("  nothing new appeared. Either the speaker isn't in pairing mode or is out of\n"
          "  range, or BlueZ already had it cached from an earlier scan — a cached device\n"
          "  is in the baseline too, so the difference can't reveal it. Showing everything\n"
          "  unpaired instead; `bt.py forget` clears a stale entry if you spot one.")
    return [(m, n) for m, n in after if m not in paired]


def cmd_pair(diff=False):
    if not bt_ready():
        return
    paired = {m for m, _ in _devices("Paired")}
    cands = _candidates_diff(paired) if diff else _candidates_plain(paired)
    if not cands:
        print("  no unpaired devices found — is the speaker in pairing mode?")
        return
    if not diff and len(cands) > 4:
        print(f"  ({len(cands)} candidates — `bt.py pair --diff` narrows this to the one you"
              " just switched on)")
    mac = _pick(cands, "candidate", confirm=True)
    if not mac:
        print("  nothing selected.")
        return
    for action in ("pair", "trust", "connect"):
        out = usr(f"bluetoothctl {action} {shlex.quote(mac)}", timeout=25)
        ok = action == "trust" or "successful" in out.lower()
        print(f"  {action:<8} {'✓' if ok else (out.splitlines()[-1] if out else '?')}")
    make_default(mac, announce=True)


def cmd_forget(mac=None):
    """Remove a pairing. A paired device stays *trusted*, so it can auto-reconnect and take
    the default sink back — forget the old speaker when you swap."""
    if not mac:
        mac = _pick(_devices("Paired"), "paired device")
        if not mac:
            print("nothing paired to forget")
            return
    usr(f"bluetoothctl disconnect {shlex.quote(mac)}", timeout=20)
    out = usr(f"bluetoothctl remove {shlex.quote(mac)}", timeout=20)
    print(f"  forgot {mac} ✓" if "removed" in out.lower()
          else f"  {out.splitlines()[-1] if out else '?'}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return
    if not have_board():
        sys.exit("no adb device — plug the Uno Q in over USB")

    args = sys.argv[1:]
    diff = bool({"--diff", "-d"} & set(args))
    args = [a for a in args if a not in ("--diff", "-d")]

    cmd = args[0] if args else "status"
    arg = args[1] if len(args) > 1 else None
    if cmd == "status":
        cmd_status()
    elif cmd == "connect":
        cmd_connect(arg)
    elif cmd == "disconnect":
        cmd_disconnect(arg)
    elif cmd == "pair":
        cmd_pair(diff)
    elif cmd == "forget":
        cmd_forget(arg)
    else:
        sys.exit(f"unknown command '{cmd}' — see `bt.py --help`")


if __name__ == "__main__":
    main()
