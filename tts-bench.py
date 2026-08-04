#!/usr/bin/env python3
"""tts-bench.py — measure Piper voice speed on the Arduino Uno Q, from your host over adb.

Why a separate tool: a one-shot `tts.py "..."` reloads the model each call (~1-2 s), so it
measures load+synth, not synth. This loads each voice ONCE on the board, warms it, then times a
fixed paragraph and reports the real-time factor (RTF = synth_time / audio_duration). RTF < 1
means it generates faster than playback (can stream live); RTF > 1 means it can't keep up and
you'd pre-generate. The synthesis runs on the BOARD's CPU (the device under test) — we just drive
it and print the numbers here.

  python tts-bench.py                 # benchmark every voice in ~/.local/share/piper
  python tts-bench.py "custom text to synthesize"

Run setup-tts.py first (needs piper + at least one voice installed).
"""
import shlex
import subprocess
import sys

# Runs ON THE BOARD via the pipx venv python. Loads each voice once, warms it (excludes first-call
# graph-opt overhead), times a paragraph, prints a row. Reads the test text from argv[1] (or a
# default). Self-contained — no deps beyond piper.
BOARD_BENCH = r'''
import os, sys, glob, time
from piper import PiperVoice
text = sys.argv[1] if len(sys.argv) > 1 else (
    "The quick brown fox jumps over the lazy dog. "
    "Real time speech on the Arduino Uno Q. " * 2)
vdir = os.path.expanduser("~/.local/share/piper")
onnx = sorted(glob.glob(vdir + "/*.onnx"))
if not onnx:
    print("no voices in " + vdir + " — run setup-tts.py"); sys.exit(1)
print("%-22s %8s %8s %8s %7s" % ("voice", "load", "synth", "audio", "RTF"))
for path in onnx:
    name = os.path.basename(path)[:-5]
    t = time.time(); v = PiperVoice.load(path); load = time.time() - t
    list(v.synthesize("warm up"))                 # warm: exclude first-inference overhead
    n, sr = 0, v.config.sample_rate
    t = time.time()
    for c in v.synthesize(text):
        n += len(c.audio_int16_array); sr = c.sample_rate
    synth = time.time() - t
    dur = n / sr if sr else 0
    rtf = synth / dur if dur else 0
    tag = "realtime OK" if rtf < 1 else "slower than realtime"
    print("%-22s %7.2fs %7.2fs %7.2fs %7.3f  %s" % (name, load, synth, dur, rtf, tag))
'''


def adb(args, **kw):
    return subprocess.run(["adb", *args], text=True, capture_output=True, **kw)


def main():
    text = sys.argv[1] if len(sys.argv) > 1 else ""
    if text in ("-h", "--help"):
        print(__doc__.strip())
        return

    if not any(l.strip().endswith("device") for l in adb(["devices"]).stdout.splitlines()[1:]):
        sys.exit("no adb device — plug the Uno Q in over USB")

    venvs = adb(["shell", "pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null"]).stdout.strip() \
        or "$HOME/.local/share/pipx/venvs"
    vp = f"{venvs}/piper-tts/bin/python"

    # Run the benchmark on the board, streaming results; filter the benign onnxruntime GPU-probe
    # warning so the table is clean. `python -` reads the script from stdin; the text (if any) is
    # argv[1] on the board.
    cmd = f"{vp} -"
    if text:
        cmd += " " + shlex.quote(text)
    p = subprocess.Popen(["adb", "shell", cmd],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True)
    p.stdin.write(BOARD_BENCH)
    p.stdin.close()
    for line in p.stdout:
        if "onnxruntime" in line or "GetGpuDevices" in line or "device_discovery" in line:
            continue
        sys.stdout.write(line)
        sys.stdout.flush()
    p.wait()


if __name__ == "__main__":
    main()
