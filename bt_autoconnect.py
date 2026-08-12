#!/usr/bin/env python3
"""bt_autoconnect.py — keep a Bluetooth speaker connected. Runs ON the board.

Pairing survives a reboot; a *connection* does not. `Trusted: yes` only authorises the speaker
to connect TO the board — nothing on the board ever dials OUT, so after a power cycle a paired,
trusted speaker just sits there and the board is mute until someone runs `bt.py connect` from a
computer. That's fatal for an appliance (a board plugged into a wall socket with no host).

This dials out, on a loop: if the speaker isn't connected, connect it; then check again. It
therefore also covers the speaker being switched on AFTER the board, which is the normal order
when you power up a shelf, and re-links it when a speaker idle-drops.

  BT_AUTOCONNECT_MAC      speaker to keep connected (required)
  BT_AUTOCONNECT_PERIOD   seconds between checks (default 20)

Normally run by the bt-autoconnect.service systemd --user unit. Toggle with
`bt.py autoconnect on <MAC>` / `off` from your host.
"""
import os
import subprocess
import sys
import time

MAC = os.environ.get("BT_AUTOCONNECT_MAC", "").strip()
PERIOD = int(os.environ.get("BT_AUTOCONNECT_PERIOD", "20"))


def bctl(*args, timeout=25):
    """Run bluetoothctl, time-bounded: it hangs indefinitely if bluetooth.service is down."""
    try:
        p = subprocess.run(["bluetoothctl", *args], capture_output=True, text=True,
                           timeout=timeout)
        return p.stdout + p.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return f"(bluetoothctl unavailable: {e})"


def connected():
    return "Connected: yes" in bctl("info", MAC)


def main():
    if not MAC:
        sys.exit("BT_AUTOCONNECT_MAC not set — use `bt.py autoconnect on <MAC>`")
    print(f"autoconnect: {MAC}, checking every {PERIOD}s", flush=True)
    was = None
    while True:
        ok = connected()
        if ok != was:                      # log transitions only — this loops forever
            print(f"{'connected' if ok else 'not connected'}: {MAC}", flush=True)
            was = ok
        if not ok:
            out = bctl("connect", MAC)
            if "successful" in out.lower():
                print(f"reconnected {MAC}", flush=True)
                was = True
                # PipeWire creates the sink a moment after the link comes up; let it settle
                # before the next check so we don't dial a connection that's already forming.
                time.sleep(3)
        time.sleep(PERIOD)


if __name__ == "__main__":
    main()
