#!/usr/bin/env python3
"""espeak.py — speak text on the Arduino Uno Q from your host. For rapid prototyping.

  python espeak.py "i am a well behaved script"
  python espeak.py hello there            # quotes optional — args are joined
  python espeak.py --wake "cold start"    # play a wake burst first (un-clip the first word)

Speaks via the board's espeak-ng on the default sink (your BT speaker — see bt.py / volume.py).
Handles the env gotcha for you: a one-shot `adb shell` / `ssh host cmd` is non-interactive so it never
sources ~/.bashrc and lands with no PipeWire socket → silence; this always prefixes the env.

--wake: a BT speaker amp sleeps when idle and swallows the first second or so of audio, so a cold
call can lose the opening word. --wake plays a short throwaway sound first to wake the amp. Not
needed once warm (rapid repeated calls), so it's off by default for speed.

Better than --wake, and it covers espeak too: `tts.py keepalive on` streams a sub-audible floor so
the amp never sleeps in the first place — no throwaway burst, no lost word, nothing to remember.
"""
import shlex
import sys

import board

WAKE_WAV = "/usr/share/sounds/alsa/Front_Center.wav"


def usr(cmd, timeout=30):
    return board.usr(cmd, timeout=timeout)


def have_board():
    return board.available()


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

    board.require()

    if wake:
        usr(f"sh -c 'paplay {WAKE_WAV} 2>/dev/null; sleep 0.4'")

    rc, out = usr(f"espeak-ng {shlex.quote(text)}")
    if rc:
        sys.exit(f"espeak failed: {out}\n(no sound? connect the speaker — bt.py connect)")


if __name__ == "__main__":
    main()
