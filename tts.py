#!/usr/bin/env python3
"""tts.py — speak text + manage the Piper TTS daemon on the Arduino Uno Q.

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

Keep-alive (a sub-audible floor so the speaker's amp never sleeps mid-sentence):
  tts.py keepalive                     # status
  tts.py keepalive on | off            # toggle (instant — separate from the warm daemon)
  tts.py keepalive level <n>           # floor amplitude out of 32767 (default 20)

Health:
  tts.py doctor                        # audit the whole chain + print fixes

Speaking uses the warm daemon when it's up (instant), else a one-shot piper synth (~10 s model
load). espeak.py is the instant low-quality alternative.
"""
import base64
import os
import shlex
import sys

import board

VOICE_DIR = "$HOME/.local/share/piper"
FIFO = "/run/user/$(id -u)/tts.fifo"
WAKE_WAV = "/usr/share/sounds/alsa/Front_Center.wav"
HERE = os.path.dirname(os.path.abspath(__file__))     # repo root — daemon + unit live here
UNIT = "tts-daemon.service"
DROPIN_DIR = "$HOME/.config/systemd/user/tts-daemon.service.d"
DEFAULT_VOICE = "en_US-amy-medium"
KA_UNIT = "tts-keepalive.service"
KA_DROPIN_DIR = "$HOME/.config/systemd/user/tts-keepalive.service.d"
DEFAULT_KA_LEVEL = 20


# ── board I/O (adb over USB, or ssh when $UNOQ_HOST is set — see board.py) ───
def usr(cmd, timeout=60):
    """User-session command (env-prefixed for PipeWire/systemd --user); (rc, output)."""
    return board.usr(cmd, timeout=timeout)


def sh(cmd, timeout=60):
    return board.sh(cmd, timeout=timeout)


def sc(args, timeout=60):
    return usr(f"systemctl --user {args}", timeout=timeout)


def have_board():
    return board.available()


def push(local, remote):
    return board.push(local, remote)


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
    board.require()
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
    push(os.path.join(HERE, "tts_daemon.py"), "/home/arduino/.local/bin/tts_daemon.py")
    push(os.path.join(HERE, "tts-daemon.service"),
         "/home/arduino/.config/systemd/user/tts-daemon.service")
    sh("chmod +x ~/.local/bin/tts_daemon.py")
    sc("daemon-reload")
    sc(f"enable {UNIT} >/dev/null 2>&1")
    # On by default: without it a Bluetooth speaker's amp sleeps between utterances and swallows
    # the first word or so. `tts.py keepalive off` if you'd rather let the speaker idle down.
    keepalive_install()
    sc(f"enable --now {KA_UNIT} >/dev/null 2>&1")
    print(f"  keep-alive floor on (level {keepalive_level()}) — tts.py keepalive off to stop it")
    print("  loading voice (~10 s)...")
    ok = restart_and_wait()
    print("  daemon ready ✓ — tts.py is now instant" if ok
          else "  daemon didn't come ready (tts.py daemon logs)")
    return ok


def daemon_uninstall():
    sc(f"disable --now {UNIT} >/dev/null 2>&1")
    sc(f"disable --now {KA_UNIT} >/dev/null 2>&1")
    sh("rm -f ~/.config/systemd/user/tts-daemon.service ~/.local/bin/tts_daemon.py")
    sh("rm -f ~/.config/systemd/user/tts-keepalive.service ~/.local/bin/tts_keepalive.py")
    sh(f"rm -rf {DROPIN_DIR} {KA_DROPIN_DIR}")
    sc("daemon-reload")
    sh(f"rm -f {FIFO}")
    print("daemon removed ✓")
    if ask("also remove piper-tts (pipx) and the downloaded voices?"):
        usr("pipx uninstall piper-tts")
        sh(f"rm -rf {VOICE_DIR}")
        print("piper-tts + voices removed ✓")


def write_dropin(dropin_dir, name, body):
    """Drop a systemd --user override file on the board (base64'd past the shell)."""
    b64 = base64.b64encode(body.encode()).decode()
    sh(f"mkdir -p {dropin_dir}")
    sh(f"sh -c 'echo {b64} | base64 -d > {dropin_dir}/{name}'")
    sc("daemon-reload")


def daemon_set_voice(name):
    voices = "en_US-amy-medium,en_US-amy-low" if name == "both" else name
    write_dropin(DROPIN_DIR, "voice.conf", f"[Service]\nEnvironment=TTS_VOICES={voices}\n")
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
    keepalive_status()


def daemon_logs():
    rc, out = usr("journalctl --user -u tts-daemon -n 20 --no-pager")
    print(out or "(no logs)")


# ── keep-alive ───────────────────────────────────────────────────────────────
# A Bluetooth speaker mutes its amp after a few seconds of silence and takes ~1 s to wake, which
# eats the start of a short utterance. Its own service, not part of the TTS daemon, for two
# reasons: toggling it is then instant (restarting the daemon costs a ~10 s voice reload), and it
# helps anything that plays audio — espeak.py, paplay, a WAV — not just piper.
def keepalive_install():
    sh("mkdir -p ~/.local/bin ~/.config/systemd/user")
    push(os.path.join(HERE, "tts_keepalive.py"), "/home/arduino/.local/bin/tts_keepalive.py")
    push(os.path.join(HERE, "tts-keepalive.service"),
         "/home/arduino/.config/systemd/user/tts-keepalive.service")
    sh("chmod +x ~/.local/bin/tts_keepalive.py")
    sc("daemon-reload")


def keepalive_level():
    """The configured floor — a drop-in override if set, else the script's own default."""
    for tok in sc("show -p Environment --value tts-keepalive")[1].split():
        if tok.startswith("TTS_KEEPALIVE_LEVEL="):
            return tok.split("=", 1)[1]
    return str(DEFAULT_KA_LEVEL)


def keepalive_status():
    active = sc("is-active tts-keepalive")[1]
    print(f"keepalive : {active} / {sc('is-enabled tts-keepalive')[1]}   "
          f"level {keepalive_level()} of 32767")


_KEEPALIVE_USAGE = "usage: tts.py keepalive [status|on|off|level <n>]"


def do_keepalive(args):
    verb = args[0] if args else "status"
    if verb == "status":
        keepalive_status()
    elif verb == "on":
        keepalive_install()                       # push first — `on` after an edit picks it up
        rc, out = sc(f"enable --now {KA_UNIT}")
        print(f"keepalive on ✓ (level {keepalive_level()})" if not rc else f"failed: {out}")
    elif verb == "off":
        rc, out = sc(f"disable --now {KA_UNIT}")
        print("keepalive off ✓ (expect the first word to clip again)" if not rc
              else f"failed: {out}")
    elif verb == "level" and len(args) >= 2:
        try:
            level = int(args[1])
        except ValueError:
            sys.exit(f"level must be a number out of 32767 — {_KEEPALIVE_USAGE}")
        if not 0 < level < 32768:
            sys.exit("level must be 1..32767 (to turn it off: tts.py keepalive off)")
        write_dropin(KA_DROPIN_DIR, "level.conf",
                     f"[Service]\nEnvironment=TTS_KEEPALIVE_LEVEL={level}\n")
        if sc("is-active tts-keepalive")[1] == "active":
            sc(f"restart {KA_UNIT}")
        print(f"keepalive level → {level}"
              f"{'' if level <= 60 else '  (may be audible as hiss)'}")
    else:
        sys.exit(_KEEPALIVE_USAGE)


_DAEMON_USAGE = ("usage: tts.py daemon "
                 "status|start|stop|restart|install|uninstall|voice <name|both>|logs")


def do_daemon(args):
    if not args:
        sys.exit(_DAEMON_USAGE)
    board.require()
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
    board.require()

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

    ka = sc("is-active tts-keepalive")[1] == "active"
    row(ka, "keep-alive", f"level {keepalive_level()}" if ka else "off — first word will clip",
        "tts.py keepalive on")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return
    if args[0] == "daemon":
        do_daemon(args[1:])
    elif args[0] == "keepalive":
        board.require()
        do_keepalive(args[1:])
    elif args[0] == "doctor":
        doctor()
    else:
        do_speak(args)


if __name__ == "__main__":
    main()
