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
import os
import signal
import subprocess
import sys

from piper import PiperVoice

VOICE_DIR = os.path.expanduser("~/.local/share/piper")
# Which voices to hold warm — comma list, default the single nicer voice. First = default.
WANTED = [v.strip() for v in os.environ.get("TTS_VOICES", "en_US-amy-medium").split(",") if v.strip()]
DEFAULT_VOICE = WANTED[0]
FIFO = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "tts.fifo")
# Silence written ahead of every utterance. Each line gets a fresh paplay, so the sink resumes
# from idle and (on Bluetooth) the speaker unmutes its amp — anything played during that ramp is
# lost, which eats the opening word. The lead-in absorbs it instead. Raise it if a speaker still
# clips; TTS_LEAD_MS=0 disables. (This is per-utterance, so it also costs that much latency.)
LEAD_IN_MS = int(os.environ.get("TTS_LEAD_MS", "400"))


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
    player = subprocess.Popen(
        ["paplay", "--raw", f"--rate={sr}", "--format=s16le", "--channels=1"],
        stdin=subprocess.PIPE)
    try:
        if LEAD_IN_MS:
            player.stdin.write(b"\x00\x00" * (sr * LEAD_IN_MS // 1000))   # s16le mono silence
        for chunk in v.synthesize(text):
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
