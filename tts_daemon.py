#!/usr/bin/env python3
"""tts_daemon.py — keep Piper voices warm and speak text on demand. Runs ON the board.

Loading a Piper model costs ~10 s on the QRB2210, which makes per-call TTS painful. This daemon
loads its voice ONCE (~110 MB warm for amy-medium) and then speaks lines fed to a FIFO — turning
~11 s/call into just the synth time (RTF ~0.4, faster than realtime). Audio plays through the
PipeWire default sink (your BT speaker) via paplay.

Loads en_US-amy-medium by default; set TTS_VOICES (comma list) to load others / more than one
(each ~100 MB warm — both amy voices is ~260 MB, fine on this board). With several loaded you can
pick per line; with one, the voice prefix is ignored.

Driven by a FIFO (fire-and-forget): write a line to $XDG_RUNTIME_DIR/tts.fifo —
  "hello there"                 # default voice
  "en_US-amy-low:be quick"      # pick a loaded voice by name (prefix before the first ':')

Normally launched by the tts-daemon.service systemd --user unit (which provides XDG_RUNTIME_DIR +
the dbus session so paplay reaches the default sink). Run it with the pipx venv python (has piper).
"""
import atexit
import math
import os
import signal
import struct
import subprocess
import sys

from piper import PiperVoice

VOICE_DIR = os.path.expanduser("~/.local/share/piper")
# Which voices to hold warm — comma list, default the single nicer voice. First = default.
WANTED = [v.strip() for v in os.environ.get("TTS_VOICES", "en_US-amy-medium").split(",") if v.strip()]
DEFAULT_VOICE = WANTED[0]
FIFO = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "tts.fifo")
# Optional lead-in tone ahead of every utterance, to absorb the wake-up ramp of a Bluetooth
# speaker's amp. OFF by default now: tts-keepalive.service solves the same problem better, by
# streaming a sub-audible floor so the amp never sleeps in the first place — no added latency and
# nothing audible before the speech.
#
# The lead-in stays as a fallback for a speaker the keep-alive can't hold, but note what the
# measurement showed: this amp needs ~1.3 s to wake ("one two three four..." started at "three"),
# so a lead-in has to be at least that long to work at all — i.e. an audible tone and 1.3 s of
# latency before every phrase. Silence won't do it; a level detector needs real energy.
#
# TTS_LEAD_MS   length (0 = off, the default; it's added latency as well as padding)
# TTS_LEAD_LEVEL amplitude out of 32767 (0 = silence, which does NOT wake an amp)
# TTS_LEAD_HZ   pitch — low enough to be unobtrusive, high enough for a small speaker to render
LEAD_IN_MS = int(os.environ.get("TTS_LEAD_MS", "0"))
LEAD_LEVEL = int(os.environ.get("TTS_LEAD_LEVEL", "700"))
LEAD_HZ = int(os.environ.get("TTS_LEAD_HZ", "220"))


def lead_in(sr):
    """s16le mono wake-up lead-in at the voice's sample rate."""
    n = sr * LEAD_IN_MS // 1000
    if n <= 0:
        return b""
    if LEAD_LEVEL <= 0:
        return b"\x00\x00" * n
    return b"".join(
        struct.pack("<h", int(LEAD_LEVEL * (1.0 - i / n) * math.sin(2 * math.pi * LEAD_HZ * i / sr)))
        for i in range(n))


def load_voices():
    voices = {}
    for name in WANTED:
        path = os.path.join(VOICE_DIR, name + ".onnx")
        if not os.path.exists(path):
            print(f"skip {name}: not in {VOICE_DIR} (run setup-tts.py)", flush=True)
            continue
        print(f"loading {name} ...", flush=True)
        v = PiperVoice.load(path)
        list(v.synthesize("warm up"))          # warm: pay the first-inference cost now
        voices[name] = v
    return voices


def speak(voices, voice, text):
    v = voices.get(voice) or voices.get(DEFAULT_VOICE) or next(iter(voices.values()))
    sr = v.config.sample_rate
    # Pull the first chunk BEFORE opening the stream, so playback never starts ahead of the
    # synthesizer. Otherwise paplay opens, underruns while Piper is still working, and that gap
    # of true silence lets a speaker's amp fall back asleep mid-utterance. (Measured on the sink
    # monitor: a 240 ms hole ahead of the speech.) Synthesis runs faster than realtime, so once
    # the first chunk is buffered the rest keeps ahead of playback and the stream stays continuous.
    chunks = iter(v.synthesize(text))
    first = next(chunks, None)
    if first is None:
        return
    player = subprocess.Popen(
        ["paplay", "--raw", f"--rate={sr}", "--format=s16le", "--channels=1"],
        stdin=subprocess.PIPE)
    try:
        player.stdin.write(lead_in(sr))
        player.stdin.write(first.audio_int16_bytes)
        for chunk in chunks:
            player.stdin.write(chunk.audio_int16_bytes)
    finally:
        player.stdin.close()
        player.wait()


def rm_fifo():
    try:
        os.unlink(FIFO)
    except FileNotFoundError:
        pass


def main():
    # The FIFO's existence is the readiness signal, so clear any stale one from a previous
    # instance BEFORE the (~10 s) load, and remove ours on exit — so `test -p FIFO` is true only
    # while a warm daemon is actually serving.
    rm_fifo()
    atexit.register(rm_fifo)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))   # systemd stop → triggers atexit

    voices = load_voices()
    if not voices:
        sys.exit(f"no voices in {VOICE_DIR} — run setup-tts.py")
    # All downloaded voice names (not just loaded) — so a known voice prefix is always stripped,
    # even if that voice isn't warm (we fall back to the default); a colon in real text is left
    # alone because its prefix won't match a voice name.
    known = {f[:-5] for f in os.listdir(VOICE_DIR) if f.endswith(".onnx")}
    os.mkfifo(FIFO)                              # only now, after warm
    print(f"ready: {', '.join(voices)}  (default {DEFAULT_VOICE})  fifo={FIFO}", flush=True)

    while True:                                 # reopen on each writer EOF (FIFO server loop)
        with open(FIFO) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                voice, text = DEFAULT_VOICE, line
                if ":" in line and line.split(":", 1)[0] in known:
                    name, text = line.split(":", 1)
                    voice = name if name in voices else DEFAULT_VOICE
                try:
                    speak(voices, voice, text)
                except Exception as e:          # never let one bad line kill the daemon
                    print(f"speak error: {e}", flush=True)


if __name__ == "__main__":
    main()
