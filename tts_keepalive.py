#!/usr/bin/env python3
"""tts_keepalive.py — keep the Bluetooth speaker's amp awake. Runs ON the board.

A cheap Bluetooth speaker mutes its output stage after a few seconds of silence (class-D
signal-sense standby, to save battery and hide idle hiss) and takes ~1 s to come back. Whatever
plays during that ramp is lost, so a short announcement is heard from the middle: on the S-SOUND
here, "one two three four..." reliably started at "three" — about 1.3 s gone.

There is no protocol message for "wake up", so the only fix is to never go silent. This streams a
continuous sub-audible noise floor to the default sink; the amp stays on and speech starts
instantly, with no lead-in tone before it.

Measured on the S-SOUND, and the reason the usual "loop a silent WAV" advice fails here: the
speaker's detector is LEVEL-based, not digital-zero-based. A floor of amplitude 2 (~-84 dBFS) did
not hold the amp awake; 20 (~-64 dBFS) does, and is inaudible in a quiet room. Broadband noise has
a narrow window (150 is plainly audible as hiss), so if yours needs more energy than it can hide,
switch to the tone mode: a 60 Hz sine at level 600 also holds the amp, and a small driver can't
reproduce it.

  TTS_KEEPALIVE_LEVEL  s16 amplitude out of 32767 (default 20; 0 = off)
  TTS_KEEPALIVE_MODE   noise | tone   (default noise)
  TTS_KEEPALIVE_HZ     tone frequency (default 60)

Normally run by the tts-keepalive.service systemd --user unit, which provides XDG_RUNTIME_DIR +
the dbus session so paplay reaches the default sink. Toggle it with `tts.py keepalive on|off`.
"""
import math
import os
import struct
import subprocess
import sys
import time

RATE = 48000
LEVEL = int(os.environ.get("TTS_KEEPALIVE_LEVEL", "20"))
MODE = os.environ.get("TTS_KEEPALIVE_MODE", "noise")
HZ = int(os.environ.get("TTS_KEEPALIVE_HZ", "60"))


def block():
    """One second of floor, written on repeat (whole cycles, so a tone loops seamlessly)."""
    buf = bytearray()
    if MODE == "tone":
        for i in range(RATE):
            buf += struct.pack("<h", int(LEVEL * math.sin(2 * math.pi * HZ * i / RATE)))
    else:
        raw = os.urandom(RATE)
        for i in range(RATE):
            buf += struct.pack("<h", (raw[i] - 128) * LEVEL // 128)
    return bytes(buf)


def main():
    if LEVEL <= 0:
        sys.exit("TTS_KEEPALIVE_LEVEL=0 — nothing to play (disable the unit instead)")
    floor = block()
    print(f"keepalive: {MODE} level {LEVEL}"
          f"{f' @ {HZ} Hz' if MODE == 'tone' else ''} -> default sink", flush=True)
    # No --device, deliberately: an untargeted stream is one PipeWire's pulse layer will MOVE when
    # the default sink changes, so a keep-alive started before the speaker connected follows it
    # onto the Bluetooth sink (confirmed after a reboot: the stream migrated off the headphone
    # jack on `bt.py connect`). The respawn loop covers the other case — a disconnect destroys the
    # sink outright and takes paplay with it. Writing blocks until paplay consumes, so the loop
    # paces itself at realtime with no sleep.
    while True:
        p = subprocess.Popen(
            ["paplay", "--raw", f"--rate={RATE}", "--format=s16le", "--channels=1",
             "--stream-name=tts-keepalive"],
            stdin=subprocess.PIPE)
        try:
            while True:
                p.stdin.write(floor)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                p.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            p.wait()
        print("keepalive: sink went away, retrying", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
