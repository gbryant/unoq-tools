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
| `tts.py` | speak text from the host; `tts.py daemon status\|start\|stop\|restart\|install\|uninstall\|voice\|logs`; `tts.py keepalive on\|off\|level <n>`; `tts.py doctor` |
| `tts_daemon.py` | the daemon itself (runs on the board; `tts-daemon.service` systemd --user unit) |
| `tts_keepalive.py` | sub-audible floor that stops the speaker's amp sleeping (`tts-keepalive.service`) |
| `tts-bench.py` | measure voice speed (real-time factor) per voice |
| `espeak.py` | zero-setup fallback: speak via espeak-ng |

Any on-board program can speak by writing a line to the FIFO (`"text"` or
`"voice-name:text"`) — that's the whole client contract, and the FIFO's
existence signals a warm daemon.

**Clipped first word?** A Bluetooth speaker mutes its amp after a few seconds of
silence and takes about a second to come back, so the start of a short
announcement is simply gone. `tts-keepalive.service` fixes it by never letting the
stream go silent: it plays a continuous **sub-audible noise floor** to the default
sink, the amp stays on, and speech starts instantly. It's installed and enabled by
`tts.py daemon install`.

```bash
tts.py keepalive              # status: active / enabled, level
tts.py keepalive off          # let the speaker idle down again
tts.py keepalive on
tts.py keepalive level 40     # floor amplitude out of 32767
```

It's a separate unit from the TTS daemon on purpose — toggling is instant (a
daemon restart costs a ~10 s voice reload), and the floor helps anything that
plays audio, `espeak.py` and bare `paplay` included.

To measure what your speaker loses, speak `"one two three four five six seven
eight"` with the floor off and note the first number you hear cleanly — each is
roughly 400 ms, so starting at "three" means about 1.3 s is going missing.

Picking the level is a squeeze between two thresholds, and on the S-SOUND here
the window is narrow:

- **Silence doesn't work.** The detector is level-based, not digital-zero-based:
  a floor of amplitude 2 (~-84 dBFS) left the speech still clipped, so the usual
  "loop a silent WAV" advice fails. 20 holds the amp awake.
- **Too much is audible.** 150 is plain hiss in a quiet room. 20 is inaudible,
  60 is borderline.

If your speaker needs more energy than it can hide, switch shape rather than
level — a 60 Hz sine at level 600 also holds the amp, and a small driver can't
reproduce it:

```bash
systemctl --user edit tts-keepalive   # add:  [Service]
                                      #       Environment=TTS_KEEPALIVE_MODE=tone
systemctl --user restart tts-keepalive
```

The daemon also has a **lead-in tone** (`TTS_LEAD_MS`, default 0 — off) that pads
each utterance instead. It's the older fix and a worse one: to cover a 1.3 s wake
it has to be 1.3 s of *audible* tone before every phrase, paying that latency
every time. Use it only for a speaker the keep-alive can't hold.

See also the no-suspend drop-in in
[docs/unoq-bluetooth-audio.md](docs/unoq-bluetooth-audio.md) §3b — that fixes the
larger version of this problem (whole clips lost after 5 s idle).

## Bluetooth audio

| Tool | What it does |
|------|--------------|
| `setup-bt-audio.py` | wizard for headless BT audio: PipeWire-first, pairing |
| `bt.py` | connect / pair / forget / report BT audio devices; `bt.py autoconnect on <MAC>` keeps one connected |
| `bt_autoconnect.py` | the board-side dial-out loop (`bt-autoconnect.service`) |
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
./bt.py autoconnect on <MAC>   # the board keeps it connected by itself (see below)
./bt.py autoconnect off | status
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

**`autoconnect`** is what makes a board standalone — one that boots on a wall socket
with no computer attached. Pairing survives a reboot but a **connection doesn't**, and
`Trusted: yes` only authorises the speaker to dial *in*: nothing on the board dials
*out*, so after a power cycle a paired, trusted speaker sits idle and the board is
mute until someone runs `bt.py connect`. `autoconnect on <MAC>` installs a board-side
loop (a `systemd --user` unit, so linger runs it with nobody logged in) that dials out
until the speaker answers, then keeps checking — so it also handles switching the
speaker on *after* the board, and re-links one that idle-drops.

```bash
./bt.py autoconnect on 41:42:9D:48:82:BF
./bt.py autoconnect                       # active/enabled + the MAC it's dialling
adb shell 'journalctl --user -u bt-autoconnect -f'
```

It logs only transitions, so the journal stays readable across days of uptime.

## Reaching the board — USB or network

The tools speak to the board two ways, chosen by **`UNOQ_HOST`**:

```bash
volume.py 60                                  # adb over USB (the default)
UNOQ_HOST=gandalf.local volume.py 60          # ssh (user defaults to `arduino`)
UNOQ_HOST=arduino@192.168.1.54 volume.py 60   # or spell the user out
export UNOQ_HOST=gandalf.local                # once the board lives somewhere permanent
```

adb is right on the bench: USB, no network, no credentials. It's useless the moment the
board is *deployed* — running as an appliance across the room with nothing plugged into
it — and that's exactly when you want to change the volume or reconnect a speaker. Same
commands, same output, either way.

ssh needs a key, or every call prompts for a password:

```bash
ssh-copy-id arduino@gandalf.local     # once
```

Connections are multiplexed (ControlMaster), so a tool making several calls pays the
handshake once, and everything is timeout-bounded so an unreachable board fails instead
of hanging.

`volume.py`, `bt.py`, `tts.py` and `espeak.py` work over either transport. The one-time
**setup wizards** (`setup-board.py`, `setup-bt-audio.py`, `setup-tts.py`) are still
adb-only — they're bench jobs you run with the board in front of you, before it has a
network to be reached over.

## Prerequisites

`adb` on the host (`brew install --cask android-platform-tools` /
`apt install adb`) and the Uno Q on USB — or `UNOQ_HOST` set and an ssh key installed,
per above. Each tool prints what it needs beyond that when first run.
