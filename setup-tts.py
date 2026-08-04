#!/usr/bin/env python3
"""setup-tts.py — install Piper text-to-speech on the Arduino Uno Q, from your host over adb.

Wizard: inspects each step, shows state, asks before changing anything — idempotent, re-runnable.
Installs piper-tts via pipx (Debian's PEP-668-blessed isolated-venv path — no
--break-system-packages; exposes the `piper` command on ~/.local/bin), downloads a Piper voice via
piper's own canonical downloader, and verifies by speaking a test phrase through the default sink
(your BT speaker). Run setup-board.py and setup-bt-audio.py first (needs sudo for one apt + a
connected speaker to hear the test).

CPU-only Piper — no sherpa-onnx / NPU. The QRB2210's 4 cores run Piper voices fine; sherpa's only
real value-add (a CPU/GPU/NPU provider abstraction) is moot here and the NPU path is paused.

  setup-tts.py                 # install + both voices (en_US-amy-medium + en_US-amy-low)
  setup-tts.py --voice en_US-amy-low   # just one voice (any piper voice name)
"""
import getpass
import shlex
import subprocess
import sys
import threading

USER = "arduino"
UENV = 'XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus'
VOICE_DIR = "$HOME/.local/share/piper"
# pipx puts the `piper` command symlink in ~/.local/bin; prefix it for non-interactive shells.
PATHPFX = "export PATH=$HOME/.local/bin:$PATH;"


def _run(args, inp=None, timeout=60):
    try:
        return subprocess.run(args, text=True, capture_output=True, input=inp, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def _stream(args, inp=None, timeout=600, indent="    "):
    """Run a long board command, echoing its output to our terminal LIVE (so apt/pip/downloads
    don't look hung) while also teeing it into a string the caller can still inspect. A watchdog
    timer kills the process on timeout. Returns (rc, full_output)."""
    p = subprocess.Popen(args, stdin=(subprocess.PIPE if inp is not None else None),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    killer = threading.Timer(timeout, p.kill)
    killer.start()
    out = []
    try:
        if inp is not None:
            p.stdin.write(inp)
            p.stdin.close()
        for line in p.stdout:
            sys.stdout.write(indent + line)
            sys.stdout.flush()
            out.append(line)
        p.wait()
    finally:
        killer.cancel()
    return p.returncode, "".join(out)


def sh_stream(cmd, timeout=600):
    return _stream(["adb", "shell", cmd], timeout=timeout)


def sudo_stream(cmd, timeout=600):
    if _PW["v"] is None:
        _PW["v"] = getpass.getpass(f"  {USER} sudo password: ")
    return _stream(["adb", "shell", f"sudo -S -p '' {cmd}"], inp=_PW["v"] + "\n", timeout=timeout)


def sh(cmd, timeout=60):
    p = _run(["adb", "shell", cmd], timeout=timeout)
    return p.stdout.strip() if p else ""


def usr(cmd, timeout=30):
    p = _run(["adb", "shell", f"{UENV} {cmd}"], timeout=timeout)
    return (p.stdout + p.stderr).strip() if p else "(timed out)"


_PW = {"v": None}


def sudo(cmd, timeout=300):
    if _PW["v"] is None:
        _PW["v"] = getpass.getpass(f"  {USER} sudo password: ")
    p = _run(["adb", "shell", f"sudo -S -p '' {cmd}"], inp=_PW["v"] + "\n", timeout=timeout)
    if p is None:
        return 124, "(timed out)"
    return p.returncode, (p.stdout + p.stderr).strip()


def ask(prompt, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        a = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default
    return default if not a else a.startswith("y")


def have_board():
    d = _run(["adb", "devices"], timeout=10)
    return bool(d) and any(l.strip().endswith("device") for l in d.stdout.splitlines()[1:])


# pipx installs piper into an isolated venv and exposes the `piper` command on ~/.local/bin —
# Debian's PEP-668-blessed path, no --break-system-packages. `python -m piper.download_voices`
# won't work from system python though (piper's isolated), so the voice download runs through
# pipx's venv python, located here.
# ── steps ────────────────────────────────────────────────────────────────────
def step_pipx():
    if sh("command -v pipx"):
        print("pipx       : present ✓")
    else:
        print("pipx       : missing")
        if not ask("  apt install pipx?"):
            return False
        rc, _ = sudo_stream("sh -c 'apt-get update && apt-get install -y pipx'")
        if rc:
            print("  apt failed (see output above)")
            return False
        print("  installed ✓")
    # ensurepath adds ~/.local/bin to the shell profile so `piper` is found in new shells.
    sh("pipx ensurepath >/dev/null 2>&1")
    return True


def step_piper():
    if sh("pipx list 2>/dev/null | grep -i piper"):
        print("piper-tts  : installed ✓ (pipx)")
        return True
    print("piper-tts  : not installed")
    if not ask("  pipx install piper-tts?"):
        return False
    sh_stream("pipx install piper-tts", timeout=600)
    if sh("pipx list 2>/dev/null | grep -i piper"):
        print("  installed ✓")
        return True
    print("  install FAILED (see output above)")
    return False


def venv_python():
    """Path to the pipx-managed piper venv's python (has the piper module for download_voices)."""
    venvs = sh("pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null") or "$HOME/.local/share/pipx/venvs"
    return f"{venvs}/piper-tts/bin/python"


def step_path():
    # pipx ensurepath edits ~/.profile (login shells), but an interactive `ssh`/`adb shell`
    # sources ~/.bashrc — so guarantee ~/.local/bin there too, or `piper` won't be found when
    # you ssh in. (Our one-shot tools prefix PATH themselves; this is for interactive use.)
    if ".local/bin" in sh("grep '.local/bin' ~/.bashrc 2>/dev/null"):
        print("PATH       : ~/.local/bin in .bashrc ✓")
        return
    print("PATH       : adding ~/.local/bin to ~/.bashrc (so `piper` works when you ssh in)")
    sh("sh -c 'echo \"export PATH=$HOME/.local/bin:$PATH\" >> ~/.bashrc'")
    print("  added ✓")


def _have_voice(voice):
    return sh(f"test -f {VOICE_DIR}/{voice}.onnx && echo yes") == "yes"


def step_voices(voices):
    present = [v for v in voices if _have_voice(voice=v)]
    missing = [v for v in voices if v not in present]
    for v in present:
        print(f"voice      : {v} ✓")
    if not missing:
        return True
    print(f"voice      : need {', '.join(missing)}")
    if not ask(f"  download {len(missing)} voice(s) via piper (from the internet)?"):
        return bool(present)
    sh(f"mkdir -p {VOICE_DIR}")
    py = venv_python()
    for v in missing:
        print(f"  downloading {v} (piper.download_voices)...")
        # piper's canonical downloader (fetches its catalog + model from the internet itself),
        # run through pipx's venv python since piper is isolated there. --download-dir lands the
        # files exactly where tts.py looks (confirmed flag via download_voices --help).
        sh_stream(f"{py} -m piper.download_voices --download-dir {VOICE_DIR} {shlex.quote(v)}",
                  timeout=600)
        print(f"  {v} ✓" if _have_voice(v) else f"  {v} FAILED (see output above)")
    return any(_have_voice(v) for v in voices)


def step_verify(voice):
    if not ask("\nsynthesize a test phrase now?"):
        return
    onnx = f"{VOICE_DIR}/{voice}.onnx"
    out = sh(f"sh -c '{PATHPFX} echo \"piper text to speech is ready\" "
             f"| piper -m {onnx} -f /tmp/tts-test.wav' 2>&1", timeout=60)
    if sh("test -s /tmp/tts-test.wav && echo yes") != "yes":
        print(f"  synth failed: {out.splitlines()[-1] if out else '?'}")
        return
    print("  synth ✓ — playing through the default sink (paplay)")
    usr("paplay /tmp/tts-test.wav")
    print("  (silent? connect the speaker — bt.py connect; first sound after idle can clip)")


def step_daemon():
    # The warm daemon keeps a voice loaded so tts.py is instant instead of paying ~10 s/call.
    # Install logic lives in tts.py (also exposed as `tts.py daemon install`) — reuse it here.
    import tts
    if tts.daemon_running():
        print("daemon     : tts-daemon active ✓")
        if not ask("  reinstall + restart it (e.g. to pick up a change)?", default=False):
            return
    else:
        print("daemon     : not installed (keeps a voice warm → instant tts.py)")
        if not ask("  install the warm TTS daemon (systemd --user service)?"):
            return
    tts.daemon_install()


def main():
    # Default: install both qualities so you can A/B medium (nicer) vs low (faster). The first
    # is the default tts.py voice + the one the verify step speaks.
    voices = ["en_US-amy-medium", "en_US-amy-low"]
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return
    if len(args) == 2 and args[0] == "--voice":
        voices = [args[1]]
    elif args:
        sys.exit("usage: setup-tts.py [--voice <name>]")

    if not have_board():
        sys.exit("no adb device — plug the Uno Q in over USB")
    print(f"board      : {sh('hostname')}   voices: {', '.join(voices)}")
    print("Each step shows the current state and asks before changing anything.\n")

    if not step_pipx():
        return
    if not step_piper():
        return
    step_path()
    if step_voices(voices):
        step_verify(voices[0])
    step_daemon()
    print("\ndone. Speak with:  python tts.py \"hello from piper\"")


if __name__ == "__main__":
    main()
