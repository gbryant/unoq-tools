# unoq-tools

Host-side tools for the **Arduino Uno Q** (Qualcomm QRB2210 + STM32U585, Debian
on the SBC side). Everything runs from your computer **over adb** — no
keyboard/monitor on the board, no manual ssh setup first. Idempotent wizards
show the current state, ask before changing anything, and are safe to re-run.

These tools are board-generic: they don't require (or know about) any
particular firmware on the M33. They pair well with the
[commander](https://github.com/gbryant/commander) embedded shell framework —
whose Uno Q track uses them for board bring-up and voice output — but stand
alone.

## Board setup

| Tool | What it does |
|------|--------------|
| `setup-board.py` | First-boot wizard: expired-password reset, hostname, ssh, mDNS, Wi-Fi, headless slim. A stock image's only door is adb — this opens the rest. |

The `docs/` folder covers the surrounding territory: headless RAM/CPU trimming
(`unoq-linux-setup.md`), getting back to a stock image
(`unoq-factory-restore.md`), the on-board NPU/ML stack (`unoq-ml-backend.md`),
and headless Bluetooth audio in depth (`unoq-bluetooth-audio.md`).

## Text-to-speech (Piper)

Loading a Piper voice costs ~10 s on the QRB2210, so per-call TTS is painful.
The daemon loads voices once (~110 MB warm each) and speaks lines written to a
FIFO at `/run/user/<uid>/tts.fifo` — fire-and-forget, faster than realtime
(RTF ~0.4).

| Tool | What it does |
|------|--------------|
| `setup-tts.py` | installs Piper (pipx) + voices on the board |
| `tts.py` | speak text from the host; `tts.py daemon status\|start\|stop\|restart\|install\|uninstall\|voice\|logs`; `tts.py doctor` |
| `tts_daemon.py` | the daemon itself (runs on the board; `tts-daemon.service` systemd --user unit) |
| `tts-bench.py` | measure voice speed (real-time factor) per voice |
| `espeak.py` | zero-setup fallback: speak via espeak-ng |

Any on-board program can speak by writing a line to the FIFO (`"text"` or
`"voice-name:text"`) — that's the whole client contract, and the FIFO's
existence signals a warm daemon.

**Clipped first word?** Each utterance gets a fresh `paplay`, so the sink resumes
from idle and a Bluetooth speaker unmutes its amp — anything played during that
ramp is lost. The daemon writes **400 ms of silence** ahead of every line to
absorb it. If your speaker still clips, give it more:

```bash
systemctl --user edit tts-daemon      # add:  [Service]
                                      #       Environment=TTS_LEAD_MS=700
systemctl --user restart tts-daemon
```

It's per-utterance latency as well as padding, so don't set it higher than the
clipping actually needs. `TTS_LEAD_MS=0` disables it. See also the no-suspend
drop-in in [docs/unoq-bluetooth-audio.md](docs/unoq-bluetooth-audio.md) §3b —
that fixes the larger version of this problem (whole clips lost after 5 s idle).

## Bluetooth audio

| Tool | What it does |
|------|--------------|
| `setup-bt-audio.py` | wizard for headless BT audio: PipeWire-first, pairing, auto-reconnect |
| `bt.py` | connect / pair / forget / report BT audio devices |
| `volume.py` | report or set the default sink's volume |

Run `setup-bt-audio.py` once per board — it installs the packages and applies the
headless gotcha this whole thing exists for: with no active logind seat, WirePlumber
won't start its bluez monitor, so a speaker connects but plays nothing.

After that `bt.py` is the daily tool:

```bash
./bt.py                 # what's paired, what's connected, the default sink
./bt.py connect         # reconnect a speaker that idle-disconnected
./bt.py pair            # scan, pick, pair+trust+connect, set default, speak a test word
./bt.py pair --diff     # can't tell which entry is yours? see below
./bt.py forget [MAC]    # drop a pairing so it stops auto-reconnecting
```

**`pair --diff`** is for a speaker you can't pick out of the list — a brandless one
advertising a bare MAC, or a busy RF neighbourhood where a scan returns a dozen
entries. It scans with the speaker **off** to take a baseline, scans again with it
**on**, and offers only the difference. Usually that's exactly one device, so there's
nothing to identify by eye.

If the diff comes back empty, the speaker was probably already in BlueZ's cache from
an earlier scan — a cached device is in the baseline too, so the difference can't
reveal it. The tool says so and falls back to the full unpaired list; `bt.py forget`
clears a stale entry.

When you **swap** speakers, forget the old one. A paired device stays *trusted*, so it
can wake up, auto-reconnect, and take the default sink back mid-test.

## Prerequisites

`adb` on the host (`brew install --cask android-platform-tools` /
`apt install adb`) and the Uno Q on USB. Each tool prints what it needs beyond
that when first run.
