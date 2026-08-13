#!/usr/bin/env python3
"""board.py — how every tool in this repo reaches the Uno Q: over adb, or over ssh.

adb is right on the bench (USB, no network, no credentials). It's useless once the board is
*deployed* — sitting by the TV running as an appliance, with nothing plugged into it. Then you
want ssh, and you still want `volume.py 60` rather than remembering a wpctl incantation.

Pick the transport with **$UNOQ_HOST**:

    volume.py 60                                   # adb over USB (the default)
    UNOQ_HOST=gandalf.local volume.py 60           # ssh (user defaults to `arduino`)
    UNOQ_HOST=arduino@192.168.1.54 volume.py 60    # or spell out the user

Put it in your shell profile once the board lives somewhere permanent:

    export UNOQ_HOST=gandalf.local

ssh wants a key — `ssh-copy-id arduino@gandalf.local` once, or every call prompts for a
password. Sessions are multiplexed over one connection (ControlMaster), so a tool making
several calls pays the handshake once.

Both transports run commands as `arduino` on the board and behave identically, including the
environment gotcha: neither an `adb shell` one-shot NOR `ssh host 'cmd'` is an interactive or
login shell, so neither sources ~/.bashrc, and both land with no XDG_RUNTIME_DIR / DBUS session
— i.e. pactl, paplay, espeak and systemctl --user all reach nothing. usr() prefixes the env for
you; sh() is for commands that don't need the user session.
"""
import os
import subprocess

USER = "arduino"
UENV = ('XDG_RUNTIME_DIR=/run/user/$(id -u) '
        'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus')

# Reuse one ssh connection for a tool's whole run, and don't hang on a board that's asleep or
# off the network — a wedged tool is worse than a failed one.
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "ControlMaster=auto",
            "-o", "ControlPath=/tmp/.unoq-ssh-%r@%h:%p",
            "-o", "ControlPersist=60"]


def host():
    """The ssh target, or None when we're going over adb."""
    h = os.environ.get("UNOQ_HOST", "").strip()
    if not h:
        return None
    return h if "@" in h else f"{USER}@{h}"


def over_ssh():
    return host() is not None


def where():
    """Human description of the current transport, for error messages."""
    return f"ssh {host()}" if over_ssh() else "adb (USB)"


def shell_prefix():
    """How the user would run a board command by hand, for copy-pasteable advice."""
    return f"ssh {host()}" if over_ssh() else "adb shell"


def _argv(cmd):
    return ["ssh", *SSH_OPTS, host(), cmd] if over_ssh() else ["adb", "shell", cmd]


def run(cmd, timeout=60, inp=None):
    """Run a board command; returns (rc, combined output). rc 124 on timeout."""
    try:
        p = subprocess.run(_argv(cmd), capture_output=True, text=True,
                           input=inp, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "(timed out)"
    except FileNotFoundError as e:
        return 127, f"({e})"
    return p.returncode, (p.stdout + p.stderr).strip()


def sh(cmd, timeout=60):
    """A plain board command (no user session needed)."""
    return run(cmd, timeout=timeout)


def usr(cmd, timeout=60):
    """A command needing the user session — PipeWire, bluetoothctl, systemctl --user."""
    return run(f"{UENV} {cmd}", timeout=timeout)


def push(local, remote, timeout=120):
    """Copy a host file to the board. `adb push` / `scp`, same signature either way."""
    if over_ssh():
        argv = ["scp", *SSH_OPTS, local, f"{host()}:{remote}"]
    else:
        argv = ["adb", "push", local, remote]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "(timed out)"
    return p.returncode, (p.stdout + p.stderr).strip()


def available():
    """Is the board reachable over the selected transport?"""
    if over_ssh():
        return run("true", timeout=12)[0] == 0
    d = subprocess.run(["adb", "devices"], capture_output=True, text=True)
    return any(l.strip().endswith("device") for l in d.stdout.splitlines()[1:])


def require():
    """Exit with advice if the board isn't reachable. Returns nothing; call it early."""
    if available():
        return
    if over_ssh():
        raise SystemExit(
            f"can't reach the board over ssh ({host()}).\n"
            "  • is it powered and on the network?  ping it by name first\n"
            "  • key installed?  ssh-copy-id " + host() + "\n"
            "  • wrong host?  UNOQ_HOST is set to " + os.environ.get("UNOQ_HOST", ""))
    raise SystemExit(
        "no adb device — plug the Uno Q in over USB.\n"
        "  • already deployed somewhere?  reach it over the network instead:\n"
        "      export UNOQ_HOST=gandalf.local")
