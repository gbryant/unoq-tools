#!/usr/bin/env python3
"""bt.py — connect / pair / report Bluetooth audio on the Arduino Uno Q, from your host.

Targets the board's BlueZ over `bluetoothctl` (no sudo — BT ops are user-session). Most useful
daily: reconnect a speaker that idle-disconnected. Also drives the one-time pairing flow that
setup-bt-audio.py calls for its last step.

  bt.py                 status: paired devices, which are connected, the default sink
  bt.py connect [MAC]   connect (no MAC → the lone paired device, else pick) + make it default
  bt.py disconnect [MAC]
  bt.py pair            scan, pick a new speaker, pair+trust+connect, set default + test
  bt.py pair --diff     two scans (speaker off, then on) — pick from ONLY what appeared
  bt.py pair --all      include non-audio devices in the pick list
  bt.py forget [MAC]    disconnect + remove a pairing, so it can't auto-reconnect
  bt.py autoconnect on <MAC> | off | status
                        keep a speaker connected from the board itself — for a headless
                        board that boots with no computer attached (see below)

`pair` lists only what BlueZ classes as an audio device, which is what makes it usable in a
crowded RF neighbourhood — a scan here turns up ~25 devices, all but three of them LE beacons
advertising no name. `--all` lifts the filter for a speaker that reports no class.

`pair --diff` is for the speaker whose name you can't recognise — a brandless one advertising
a bare MAC. It scans once with the speaker off to take a baseline, once with it on, and offers
the difference. Usually unnecessary now the list is filtered, and note that LE devices rotate
their addresses between scans, so `--diff` on its own sees them all as new.

`autoconnect` is what makes a board standalone. Pairing survives a reboot but a *connection*
doesn't, and `Trusted: yes` only lets the speaker dial IN — nothing on the board dials OUT, so
after a power cycle a paired speaker sits idle and the board is mute until someone runs
`bt.py connect` from a computer. `autoconnect on <MAC>` installs a board-side loop that dials
out until the speaker answers, so power alone is enough. It also handles switching the speaker
on after the board, and re-links one that idle-drops.

Needs the board reachable (adb over USB, or ssh with $UNOQ_HOST set — see board.py) and
`bluetooth.service` running (setup-bt-audio.py enables it).
"""
import base64
import os
import shlex
import sys
import time

import board

HERE = os.path.dirname(os.path.abspath(__file__))     # repo root — the loop + unit live here
AC_UNIT = "bt-autoconnect.service"
AC_DROPIN_DIR = "$HOME/.config/systemd/user/bt-autoconnect.service.d"


# ── board I/O (timeout-bounded — bluetoothctl hangs if the daemon is down) ───
def usr(cmd, timeout=20):
    return board.usr(cmd, timeout=timeout)[1]


def have_board():
    return board.available()


def ask(prompt, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        a = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default
    return default if not a else a.startswith("y")


# ── device queries ───────────────────────────────────────────────────────────
# Deliberately NOT `bluetoothctl devices Paired` — that filter argument arrives in BlueZ 5.65,
# and 5.64 (JetPack 6.2.3) takes no argument at all: the extra word is a hard error, "Too many
# arguments". A parser looking for "Device ..." lines skips that quietly and returns an EMPTY
# list, so every paired speaker looks unpaired — status reports nothing paired, and `pair`
# offers speakers you already own as fresh candidates. `info` has been there since forever and
# carries the same state, so we ask per device instead.
class Dev:
    """One device BlueZ knows about, with the state `devices <filter>` would have given."""

    def __init__(self, mac, name):
        self.mac, self.name = mac, name
        self.paired = self.trusted = self.connected = False
        self.icon = self.cls = ""

    @property
    def audio(self):
        """A speaker/headset rather than an LE beacon. Two signals, because either can be
        absent: BlueZ's own Icon, and the Class of Device major class (0x04 = Audio/Video),
        which only classic BR/EDR devices report at all."""
        if self.icon.startswith("audio-"):
            return True
        try:
            return (int(self.cls, 16) >> 8) & 0x1f == 0x04
        except ValueError:
            return False

    # BlueZ's icon names are freedesktop icon names, not English — "audio-card" is what it
    # calls a plain speaker, which reads as nonsense in a pick list.
    KINDS = {"audio-card": "speaker", "audio-headset": "headset",
             "audio-headphones": "headphones", "audio-input-microphone": "microphone"}

    @property
    def kind(self):
        """speaker / headset / headphones — for labelling a pick list."""
        return self.KINDS.get(self.icon, self.icon.split("audio-", 1)[-1] or "audio")


def devices(scan_secs=0):
    """Every device BlueZ knows, as [Dev]. scan_secs > 0 discovers first, in the same shell.

    One board command, never two: devices a scan turns up are TEMPORARY. BlueZ drops an
    unpaired one ~30 s after discovery stops, and `info` on a dropped MAC prints nothing at
    all — so the scan and the interrogation have to share a shell, or the candidates
    evaporate in the gap between two calls.

    Wrapped in `sh -c` because usr() prefixes `VAR=... VAR=... <cmd>`, and that form exports
    to the FIRST command only — the `info` calls inside the loop would run bare. Handing the
    prefix to a shell puts the vars in its environment, where the whole script inherits them.
    """
    scan = f"bluetoothctl --timeout {scan_secs} scan on >/dev/null 2>&1; " if scan_secs else ""
    script = (f'{scan}bluetoothctl devices | while read -r _ mac rest; do '
              'printf "@@%s\\t%s\\n" "$mac" "$rest"; bluetoothctl info "$mac"; done')
    devs, cur = [], None
    for line in usr(f"sh -c '{script}'", timeout=90 + scan_secs).splitlines():
        if line.startswith("@@"):
            mac, _, name = line[2:].partition("\t")
            mac = mac.strip()
            cur = Dev(mac, name.strip() or mac)
            devs.append(cur)
            continue
        if cur is None:
            continue
        key, _, val = line.strip().partition(": ")
        val = val.strip()
        if key == "Paired":
            cur.paired = val == "yes"
        elif key == "Trusted":
            cur.trusted = val == "yes"
        elif key == "Connected":
            cur.connected = val == "yes"
        elif key == "Icon":
            cur.icon = val
        elif key == "Class":
            cur.cls = val
    return devs


def _pairs(devs, attr):
    """[(mac, name), ...] for the devices where `attr` is true — what _pick() wants."""
    return [(d.mac, d.name) for d in devs if getattr(d, attr)]


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
    paired = [d for d in devices() if d.paired]
    if not paired:
        print("no paired devices — run `bt.py pair`")
    else:
        print("paired:")
        for d in paired:
            state = "● connected" if d.connected else "○ disconnected"
            print(f"  {state}  {d.name} [{d.mac}]")
    print(f"default sink: {default_sink()}")


def cmd_connect(mac=None):
    if not bt_ready():
        return
    if not mac:
        mac = _pick(_pairs(devices(), "paired"), "paired device")
        if not mac:
            print("nothing to connect (no paired device / no pick) — try `bt.py pair`")
            return
    out = usr(f"bluetoothctl connect {shlex.quote(mac)}", timeout=25)
    if "successful" in out.lower():
        print(f"  connected {mac} ✓")
        make_default(mac)
    else:
        print(f"  connect failed: {out.splitlines()[-1] if out else '?'}")


# ── auto-connect (headless / appliance) ──────────────────────────────────────
# Pairing survives a reboot, a connection doesn't, and `Trusted: yes` only authorises the
# speaker to dial IN — so a board that boots with nobody at a keyboard stays mute. This runs a
# board-side loop that dials OUT until the speaker answers.
def _sh(cmd, timeout=20):
    return board.sh(cmd, timeout=timeout)[1]


def _sc(args, timeout=20):
    return usr(f"systemctl --user {args}", timeout=timeout)


def autoconnect_mac():
    """The MAC the board is configured to dial, from the unit's drop-in."""
    for tok in _sc("show -p Environment --value bt-autoconnect").split():
        if tok.startswith("BT_AUTOCONNECT_MAC="):
            return tok.split("=", 1)[1]
    return ""


def cmd_autoconnect(action=None, mac=None):
    if action in (None, "status"):
        active = _sc("is-active bt-autoconnect")
        target = autoconnect_mac()
        print(f"autoconnect : {active} / {_sc('is-enabled bt-autoconnect')}"
              f"{f'   {target}' if target else '   (no MAC set)'}")
        return
    if action == "off":
        _sc(f"disable --now {AC_UNIT}")
        print("autoconnect off ✓ (reconnect by hand with `bt.py connect`)")
        return
    if action != "on":
        sys.exit("usage: bt.py autoconnect [status|on <MAC>|off]")

    if not mac:
        mac = _pick(_pairs(devices(), "paired"), "paired device")
        if not mac:
            sys.exit("no paired device to auto-connect — run `bt.py pair` first")
    _sh("mkdir -p ~/.local/bin ~/.config/systemd/user")
    board.push(os.path.join(HERE, "bt_autoconnect.py"),
               "/home/arduino/.local/bin/bt_autoconnect.py")
    board.push(os.path.join(HERE, AC_UNIT),
               f"/home/arduino/.config/systemd/user/{AC_UNIT}")
    _sh("chmod +x ~/.local/bin/bt_autoconnect.py")
    body = f"[Service]\nEnvironment=BT_AUTOCONNECT_MAC={mac}\n"
    b64 = base64.b64encode(body.encode()).decode()
    _sh(f"mkdir -p {AC_DROPIN_DIR}")
    _sh(f"sh -c 'echo {b64} | base64 -d > {AC_DROPIN_DIR}/mac.conf'")
    _sc("daemon-reload")
    _sc(f"enable --now {AC_UNIT}")
    print(f"autoconnect on ✓ — the board will keep {mac} connected by itself,")
    print("  including after a reboot with no computer attached.")


def cmd_disconnect(mac=None):
    if not mac:
        mac = _pick(_pairs(devices(), "connected"), "connected device")
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
              f"       {board.shell_prefix()} 'sudo systemctl enable --now bluetooth'")
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
              f"    {board.shell_prefix()} 'dmesg | grep -i hci | tail'\n"
              "  ('crash the soc' / 'Change address cmd failed' / bluetoothd 'Invalid Index').\n"
              "  Recovery is a board reboot — power-cycling the UART-attached chip:\n"
              f"    {board.shell_prefix()} 'sudo reboot'   (adb reboot silently does nothing:\n"
              "                                        adbd runs as `arduino`, not root)\n"
              "  NOTE: while it's down BlueZ reports NO paired devices, so anything you had\n"
              "  paired will look unpaired. Don't re-pair; reboot first.")
        return False
    return True


def _scan(secs=SCAN_SECS):
    """Discovery pass; returns the [Dev] BlueZ knows immediately afterwards."""
    usr("bluetoothctl power on")
    return devices(scan_secs=secs)


def _candidates_plain(paired):
    print(f"  put the speaker in pairing mode, then scanning {SCAN_SECS} s...")
    return [d for d in _scan() if d.mac not in paired]


def _candidates_diff(paired):
    """Two scans bracketing the speaker being switched on; the new speaker is the set
    difference. Identifies a device you can't recognise by name."""
    input(f"  1/2 — make sure the speaker is OFF, then press Enter (scans {SCAN_SECS} s)...")
    before = {d.mac for d in _scan()}
    print(f"       baseline: {len(before)} device(s) in range")
    input("  2/2 — now switch the speaker ON / into pairing mode, then press Enter...")
    after = _scan()
    new = [d for d in after if d.mac not in before and d.mac not in paired]
    if new:
        return new
    print("  nothing new appeared. Either the speaker isn't in pairing mode or is out of\n"
          "  range, or BlueZ already had it cached from an earlier scan — a cached device\n"
          "  is in the baseline too, so the difference can't reveal it. Showing everything\n"
          "  unpaired instead; `bt.py forget` clears a stale entry if you spot one.")
    return [d for d in after if d.mac not in paired]


def _offer(cands, show_all):
    """(what to show, how many were hidden). Everything but the audio devices is noise: a scan
    here turns up ~20 LE beacons that advertise no name — so they print as their own MAC — at
    private addresses that ROTATE between scans, which is also what defeats `--diff` without
    this filter. Fall back to the whole list if nothing claims to be audio, so a speaker that
    reports no class can still be paired without reaching for --all."""
    if show_all:
        return cands, 0
    audio = [d for d in cands if d.audio]
    return (audio, len(cands) - len(audio)) if audio else (cands, 0)


def cmd_pair(diff=False, show_all=False):
    if not bt_ready():
        return
    paired = {d.mac for d in devices() if d.paired}
    cands = _candidates_diff(paired) if diff else _candidates_plain(paired)
    if not cands:
        print("  no unpaired devices found — is the speaker in pairing mode?")
        return
    shown, hidden = _offer(cands, show_all)
    if hidden:
        print(f"  ({hidden} non-audio device(s) hidden — `bt.py pair --all` shows them)")
    elif not show_all and len(shown) > 4:
        print("  (nothing here reports itself as audio, so all of it is listed — if the\n"
              "   speaker isn't among these it may not be in pairing mode)")
    entries = [(d.mac, f"{d.name}  ({d.kind})" if d.audio else d.name) for d in shown]
    mac = _pick(entries, "candidate", confirm=True)
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
        mac = _pick(_pairs(devices(), "paired"), "paired device")
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
    board.require()

    args = sys.argv[1:]
    diff = bool({"--diff", "-d"} & set(args))
    show_all = "--all" in args
    args = [a for a in args if a not in ("--diff", "-d", "--all")]

    cmd = args[0] if args else "status"
    arg = args[1] if len(args) > 1 else None
    if cmd == "status":
        cmd_status()
    elif cmd == "connect":
        cmd_connect(arg)
    elif cmd == "disconnect":
        cmd_disconnect(arg)
    elif cmd == "pair":
        cmd_pair(diff, show_all)
    elif cmd == "autoconnect":
        cmd_autoconnect(arg, args[2] if len(args) > 2 else None)
    elif cmd == "forget":
        cmd_forget(arg)
    else:
        sys.exit(f"unknown command '{cmd}' — see `bt.py --help`")


if __name__ == "__main__":
    main()
