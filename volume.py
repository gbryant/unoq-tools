#!/usr/bin/env python3
"""volume.py — report or adjust the Arduino Uno Q's audio volume from your host, over adb.

Targets the DEFAULT sink (your BT speaker once it's the default — see setup-bt-audio.py), via
PipeWire's `wpctl`. No sudo (volume is user-session). Caps at 100% so you can't over-amplify
into distortion.

  volume.py            report current volume + mute + sink
  volume.py 70         set to 70%
  volume.py +10        raise 10%
  volume.py -10        lower 10%
  volume.py mute | unmute | toggle
"""
import subprocess
import sys

UENV = 'XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus'
SINK = "@DEFAULT_AUDIO_SINK@"


def usr(cmd, timeout=15):
    try:
        p = subprocess.run(["adb", "shell", f"{UENV} {cmd}"],
                          capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "(timed out)"
    return (p.stdout + p.stderr).strip()


def have_board():
    devs = subprocess.run(["adb", "devices"], capture_output=True, text=True)
    return any(l.strip().endswith("device") for l in devs.stdout.splitlines()[1:])


def report():
    out = usr(f"wpctl get-volume {SINK}")          # "Volume: 0.65" or "Volume: 0.65 [MUTED]"
    if "Volume:" not in out:
        print(f"could not read volume: {out}")
        print("(is a sink active? run setup-bt-audio.py / connect a speaker)")
        return
    frac = float(out.split("Volume:")[1].split()[0])
    muted = "[MUTED]" in out
    name = usr("pactl get-default-sink") or "default sink"
    bar = "#" * round(frac * 20)
    print(f"  {name}")
    print(f"  volume {round(frac * 100):3d}%  [{bar:<20}]{'  (MUTED)' if muted else ''}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return

    if not have_board():
        sys.exit("no adb device — plug the Uno Q in over USB")

    if len(sys.argv) < 2:
        report()
        return

    arg = sys.argv[1].lower().rstrip("%")
    if arg in ("mute", "unmute", "toggle"):
        usr(f"wpctl set-mute {SINK} {'toggle' if arg == 'toggle' else ('1' if arg == 'mute' else '0')}")
    elif arg and arg[0] in "+-" and arg[1:].isdigit():
        sign = arg[0]
        limit = "" if sign == "-" else "-l 1.0"
        usr(f"wpctl set-volume {limit} {SINK} {arg[1:]}%{sign}")
    elif arg.isdigit():
        usr(f"wpctl set-volume -l 1.0 {SINK} {arg}%")
    else:
        sys.exit(f"unrecognized arg '{sys.argv[1]}' — see --help / the usage in this file")
    report()


if __name__ == "__main__":
    main()
