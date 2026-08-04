#!/usr/bin/env python3
"""tts.py — speak text + manage the Piper TTS daemon on the Arduino Uno Q, over adb.

Speak (the frequent action):
  tts.py "the robot is ready"          # quotes optional — args are joined
  tts.py --voice en_US-amy-low "..."   # one-shot voice override
  tts.py --wake "cold start"           # wake-burst first (un-clip the first word)
  tts.py --oneshot "..."               # skip the daemon, fresh synth

Daemon (so you never type a systemctl incantation):
  tts.py daemon status                 # running? voice? memory? sink?
  tts.py daemon start | stop | restart
  tts.py daemon install | uninstall
  tts.py daemon voice <name|both>      # repoint the warm voice, restart
  tts.py daemon logs

Health:
  tts.py doctor                        # audit the whole chain + print fixes

Speaking uses the warm daemon when it's up (instant), else a one-shot piper synth (~10 s model
load). espeak.py is the instant low-quality alternative.
"""
import base64
import os
import shlex
import subprocess
import sys

UENV = 'XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus'
VOICE_DIR = "$HOME/.local/share/piper"
FIFO = "/run/user/$(id -u)/tts.fifo"
WAKE_WAV = "/usr/share/sounds/alsa/Front_Center.wav"
HERE = os.path.dirname(os.path.abspath(__file__))     # repo root — daemon + unit live here
UNIT = "tts-daemon.service"
DROPIN_DIR = "$HOME/.config/systemd/user/tts-daemon.service.d"
DEFAULT_VOICE = "en_US-amy-medium"


# ── board I/O ────────────────────────────────────────────────────────────────
def _run(args, inp=None, timeout=60):
    try:
        return subprocess.run(args, text=True, capture_output=True, input=inp, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def usr(cmd, timeout=60):
    """User-session command (env-prefixed for PipeWire/systemd --user); (rc, output)."""
    p = _run(["adb", "shell", f"{UENV} {cmd}"], timeout=timeout)
    return (124, "(timed out)") if p is None else (p.returncode, (p.stdout + p.stderr).strip())


def sh(cmd, timeout=60):
    p = _run(["adb", "shell", cmd], timeout=timeout)
    return (124, "") if p is None else (p.returncode, p.stdout.strip())


def sc(args, timeout=60):
    return usr(f"systemctl --user {args}", timeout=timeout)


def have_board():
    d = _run(["adb", "devices"], timeout=10)
    return bool(d) and any(l.strip().endswith("device") for l in d.stdout.splitlines()[1:])


def adb_push(local, remote):
    return _run(["adb", "push", local, remote], timeout=60)


def ask(prompt, default=False):
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        a = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default
    return default if not a else a.startswith("y")


# ── speak ────────────────────────────────────────────────────────────────────
def daemon_running():
    return usr(f"test -p {FIFO}")[0] == 0


def speak_daemon(text, voice):
    b64 = base64.b64encode(f"{voice}:{text}".encode()).decode()
    return usr(f"sh -c 'echo {b64} | base64 -d > {FIFO}'")


def speak_oneshot(text, voice, wake):
    b64 = base64.b64encode(text.encode()).decode()
    onnx = f"{VOICE_DIR}/{voice}.onnx"
    wake_cmd = f"paplay {WAKE_WAV} 2>/dev/null; sleep 0.4; " if wake else ""
    script = (f"export PATH=$HOME/.local/bin:$PATH; "
              f"echo {b64} | base64 -d | piper -m {onnx} -f /tmp/tts.wav && "
              f"{wake_cmd}paplay /tmp/tts.wav")
    return usr(f"sh -c {shlex.quote(script)}")


def do_speak(args):
    voice, wake, oneshot = DEFAULT_VOICE, False, False
    while args and args[0].startswith("--"):
        if args[0] == "--wake":
            wake, args = True, args[1:]
        elif args[0] == "--oneshot":
            oneshot, args = True, args[1:]
        elif args[0] == "--voice" and len(args) >= 2:
            voice, args = args[1], args[2:]
        else:
            sys.exit(f"unknown option '{args[0]}' — see --help")
    text = " ".join(args).strip()
    if not text:
        sys.exit("nothing to say — usage: tts.py \"some text\"")
    if not have_board():
        sys.exit("no adb device — plug the Uno Q in over USB")
    if not oneshot and not wake and daemon_running():
        rc, out = speak_daemon(text, voice)
    else:
        rc, out = speak_oneshot(text, voice, wake)
    if rc:
        sys.exit(f"tts failed: {out}\n(no piper? run setup-tts.py. no sound? bt.py connect.)")


# ── daemon lifecycle ─────────────────────────────────────────────────────────
def wait_ready(secs=25):
    return sh(f"for i in $(seq 1 {secs}); do test -p {FIFO} && {{ echo yes; break; }}; "
              f"sleep 1; done")[1] == "yes"


def restart_and_wait():
    # Restart, then clear the FIFO from the host side so the stale one from the previous instance
    # can't read as "ready" — the new instance recreates it only after its ~10 s warm load.
    sc(f"restart {UNIT}")
    sh(f"rm -f {FIFO}")
    return wait_ready()


def daemon_install():
    """Push the daemon + unit and enable the systemd --user service. Reusable from setup-tts.py."""
    sh("mkdir -p ~/.local/bin ~/.config/systemd/user")
    adb_push(os.path.join(HERE, "tts_daemon.py"), "/home/arduino/.local/bin/tts_daemon.py")
    adb_push(os.path.join(HERE, "tts-daemon.service"),
             "/home/arduino/.config/systemd/user/tts-daemon.service")
    sh("chmod +x ~/.local/bin/tts_daemon.py")
    sc("daemon-reload")
    sc(f"enable {UNIT} >/dev/null 2>&1")
    print("  loading voice (~10 s)...")
    ok = restart_and_wait()
    print("  daemon ready ✓ — tts.py is now instant" if ok
          else "  daemon didn't come ready (tts.py daemon logs)")
    return ok


def daemon_uninstall():
    sc(f"disable --now {UNIT} >/dev/null 2>&1")
    sh("rm -f ~/.config/systemd/user/tts-daemon.service ~/.local/bin/tts_daemon.py")
    sh(f"rm -rf {DROPIN_DIR}")
    sc("daemon-reload")
    sh(f"rm -f {FIFO}")
    print("daemon removed ✓")
    if ask("also remove piper-tts (pipx) and the downloaded voices?"):
        usr("pipx uninstall piper-tts")
        sh(f"rm -rf {VOICE_DIR}")
        print("piper-tts + voices removed ✓")


def daemon_set_voice(name):
    voices = "en_US-amy-medium,en_US-amy-low" if name == "both" else name
    body = f"[Service]\nEnvironment=TTS_VOICES={voices}\n"
    b64 = base64.b64encode(body.encode()).decode()
    sh(f"mkdir -p {DROPIN_DIR}")
    sh(f"sh -c 'echo {b64} | base64 -d > {DROPIN_DIR}/voice.conf'")
    sc("daemon-reload")
    print(f"voice → {voices}, reloading (~10 s)...")
    print("ready ✓" if restart_and_wait() else "didn't come ready (tts.py daemon logs)")


def daemon_status():
    active = sc("is-active tts-daemon")[1]
    enabled = sc("is-enabled tts-daemon")[1]
    print(f"daemon : {active} / {enabled}")
    pid = sc("show -p MainPID --value tts-daemon")[1]
    if pid and pid != "0":
        rss = sh(f"ps -o rss= -p {pid}")[1]
        if rss.strip().isdigit():
            print(f"memory : {int(rss)//1024} MB (pid {pid})")
    ready = sc("show -p Environment --value tts-daemon")[1]
    print(f"voices : {ready or f'TTS_VOICES unset (default {DEFAULT_VOICE})'}")
    sink = usr("pactl get-default-sink")[1]
    bt = "bluez" in sink
    print(f"sink   : {sink} {'(BT speaker ✓)' if bt else '(not the BT speaker — bt.py connect)'}")


def daemon_logs():
    rc, out = usr("journalctl --user -u tts-daemon -n 20 --no-pager")
    print(out or "(no logs)")


_DAEMON_USAGE = ("usage: tts.py daemon "
                 "status|start|stop|restart|install|uninstall|voice <name|both>|logs")


def do_daemon(args):
    if not args:
        sys.exit(_DAEMON_USAGE)
    if not have_board():
        sys.exit("no adb device — plug the Uno Q in over USB")
    verb = args[0]
    if verb == "status":
        daemon_status()
    elif verb in ("start", "stop", "restart"):
        rc, out = sc(f"{verb} {UNIT}")
        print(f"{verb} ✓" if not rc else f"{verb} failed: {out}")
    elif verb == "install":
        daemon_install()
    elif verb == "uninstall":
        daemon_uninstall()
    elif verb == "voice" and len(args) >= 2:
        daemon_set_voice(args[1])
    elif verb == "logs":
        daemon_logs()
    else:
        sys.exit(_DAEMON_USAGE)


# ── doctor ───────────────────────────────────────────────────────────────────
def doctor():
    if not have_board():
        sys.exit("no adb device — plug the Uno Q in over USB")

    def row(ok, label, detail, fix):
        print(f"  {'✓' if ok else '✗'} {label:<14} {detail}")
        if not ok:
            print(f"      → {fix}")

    print("TTS pipeline:")
    piper_ok = sh("test -x $HOME/.local/bin/piper && echo y")[1] == "y"
    row(piper_ok, "piper", "installed" if piper_ok else "missing", "run setup-tts.py")

    voices = sh("ls $HOME/.local/share/piper/*.onnx 2>/dev/null")[1]
    names = [os.path.basename(v)[:-5] for v in voices.split()] if voices else []
    row(bool(names), "voices", ", ".join(names) or "none", "run setup-tts.py")

    active = sc("is-active tts-daemon")[1] == "active"
    ready = daemon_running()
    row(active and ready, "daemon",
        f"{'active' if active else 'inactive'}{', ready' if ready else ''}",
        "tts.py daemon install   (or: tts.py daemon start)")

    conn = usr("bluetoothctl devices Connected")[1]
    row(bool(conn), "bluetooth", conn.splitlines()[0] if conn else "no speaker connected",
        "bt.py connect")

    sink = usr("pactl get-default-sink")[1]
    row("bluez" in sink, "default sink", sink or "?",
        "pactl set-default-sink bluez_output.<MAC>.1   (or reconnect: bt.py connect)")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return
    if args[0] == "daemon":
        do_daemon(args[1:])
    elif args[0] == "doctor":
        doctor()
    else:
        do_speak(args)


if __name__ == "__main__":
    main()
