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

## Bluetooth audio

| Tool | What it does |
|------|--------------|
| `setup-bt-audio.py` | wizard for headless BT audio: PipeWire-first, pairing, auto-reconnect |
| `bt.py` | connect / pair / report BT audio devices |
| `volume.py` | report or set the default sink's volume |

## Prerequisites

`adb` on the host (`brew install --cask android-platform-tools` /
`apt install adb`) and the Uno Q on USB. Each tool prints what it needs beyond
that when first run.
