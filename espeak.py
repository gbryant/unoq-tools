#!/usr/bin/env python3
"""espeak.py — speak text on the Arduino Uno Q from your host, over adb. For rapid prototyping.

  python espeak.py "i am a well behaved script"
  python espeak.py hello there            # quotes optional — args are joined
  python espeak.py --wake "cold start"    # play a wake burst first (un-clip the first word)

Speaks via the board's espeak-ng on the default sink (your BT speaker — see bt.py / volume.py).
Handles the env gotcha for you: a one-shot `adb shell espeak-ng …` is non-interactive so it never
sources ~/.bashrc and lands with no PipeWire socket → silence; this always prefixes the env.

--wake: a BT speaker amp sleeps when idle and swallows the first ~½ s of audio, so a cold call
can lose the opening word. --wake plays a short throwaway sound first to wake the amp. Not needed
once warm (rapid repeated calls), so it's off by default for speed.
"""
import shlex
import subprocess
import sys

UENV = 'XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus'
WAKE_WAV = "/usr/share/sounds/alsa/Front_Center.wav"


def usr(cmd, timeout=30):
    try:
        p = subprocess.run(["adb", "shell", f"{UENV} {cmd}"],
                          capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "(timed out)"
    return p.returncode, (p.stdout + p.stderr).strip()


def have_board():
    d = subprocess.run(["adb", "devices"], capture_output=True, text=True)
    return any(l.strip().endswith("device") for l in d.stdout.splitlines()[1:])


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return

    wake = False
    if args[0] == "--wake":
        wake, args = True, args[1:]
    text = " ".join(args).strip()
    if not text:
        sys.exit("nothing to say — usage: espeak.py \"some text\"")

    if not have_board():
        sys.exit("no adb device — plug the Uno Q in over USB")

    if wake:
        usr(f"sh -c 'paplay {WAKE_WAV} 2>/dev/null; sleep 0.4'")

    rc, out = usr(f"espeak-ng {shlex.quote(text)}")
    if rc:
        sys.exit(f"espeak failed: {out}\n(no sound? connect the speaker — bt.py connect)")


if __name__ == "__main__":
    main()
