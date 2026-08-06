# Bluetooth Audio on the Arduino Uno Q (Debian, headless) — Setup & Troubleshooting

> Verified working 2026-06 on the on-board Debian Trixie (aarch64), hostname `gandalf`,
> user `arduino`, driven **headless over `adb shell`** (no desktop login). Audio stack:
> **PipeWire + WirePlumber** (the modern Debian default). Test speaker: Bose Micro SoundLink,
> MAC `BC:87:FA:E2:D7:0D` — substitute yours throughout.
>
> Note: earlier revisions of this guide targeted classic **PulseAudio**; current stock images
> boot **PipeWire**, so this guide is PipeWire-first. If `pactl info` reports a plain
> `pulseaudio` server (not "on PipeWire"), you're on the old stack — but don't run *both* (§7).

---

## 0. TL;DR — the one non-obvious thing

Standard Bluetooth audio setup (install, pair, route) applies — **plus one headless-specific
fix that otherwise wastes hours.** On a headless board (you reach it over `adb`/ssh with
`loginctl enable-linger`), there is **no active logind "seat."** WirePlumber gates its Bluetooth
monitor on an *active* seat, so out of the box on a headless image **the bluez monitor never
starts → no A2DP profile → the speaker pairs and "connects" but plays nothing** (`bluetoothctl
connect` fails with `br-connection-profile-unavailable`, and no `bluez_*` sink ever appears).
**Fix: one config file disabling seat-monitoring (§3).**

The two things that cost the most time, in order:
1. **WirePlumber bluez seat-monitoring** (§3) — the headless killer; without it, no BT audio at all.
2. **Session env vars per shell** (§2) — without them, `pactl`/`paplay`/`espeak` in a fresh shell
   talk to nothing. Put them in `~/.bashrc`.

A fresh-image checklist (details below): §1 packages + groups + linger → §2 env vars in `.bashrc`
→ §3 seat-monitoring fix → §4 pair the speaker → done.

**Or just run the wizard:** `setup-bt-audio.py` drives all of this from your host over
adb — inspects each step, asks before changing anything, idempotent (re-runnable). The manual
steps below are the reference / for understanding what it does.

---

## 1. One-time install (fresh image)

Stock Debian on the Uno Q ships PipeWire + WirePlumber. You need the PipeWire **Bluetooth plugin**
+ BlueZ (and a test tool):

```bash
sudo apt update
sudo apt install -y bluez libspa-0.2-bluetooth pulseaudio-utils espeak-ng
```

(`pulseaudio-utils` provides the `pactl` CLI used below — it's the pipewire-pulse compat tool,
**not** classic pulseaudio. The stock image ships without `pactl`; `wpctl` is the native
PipeWire alternative if you prefer.)

`pipewire`, `pipewire-pulse`, and `wireplumber` are normally preinstalled — verify:

```bash
pactl info | grep "Server Name"     # -> PulseAudio (on PipeWire <ver>)
```

**Do NOT install or run classic `pulseaudio` alongside PipeWire** — two stacks fight over the same
socket and you get erratic, hard-to-debug audio (§7).

**If you ran the headless slim** (`setup-board.py` / `unoq-linux-setup.md`), it **disabled
`bluetooth.service`** as an unused daemon — BT audio needs it back on, or `bluetoothctl` just
hangs (the daemon is down):

```bash
sudo systemctl enable --now bluetooth
```

Add your user to the BT/audio groups and enable **linger** (so your user's audio/BT services run
with no active login — you're on `adb`, not a desktop):

```bash
sudo usermod -aG bluetooth,audio arduino
sudo loginctl enable-linger arduino
```

Open a fresh `adb shell` after the `usermod` so the new groups take effect (`id` should list
`bluetooth` and `audio`).

---

## 2. Session env vars — put them in `~/.bashrc` (one-time)

A fresh `adb shell` has **no login session**, so `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS`
are unset — and then `pactl`/`paplay`/`espeak`/`bluetoothctl` can't find the running audio/BT
stack (`pactl` prints `Connection refused`; sound goes nowhere). Put them in `~/.bashrc` so
**every** interactive shell has them:

```bash
echo 'export XDG_RUNTIME_DIR=/run/user/$(id -u)' >> ~/.bashrc
echo 'export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus' >> ~/.bashrc
```

Why `.bashrc` and not "just run a reconnect script": a script `export`s these only inside its own
subprocess — they're gone the moment you type `espeak` back in your shell. `.bashrc` fixes every
shell once. (`enable-linger` from §1 is what keeps `/run/user/<uid>` and the user services alive
with no login, so these sockets exist for the shell to point at.)

**Important subtlety — `.bashrc` only covers *interactive* shells.** A one-shot
`adb shell espeak-ng "hi"` (or any script driving the board) is **non-interactive** and does NOT
source `.bashrc`, so the vars are empty and you get silence even after the §2 edit. For one-shot
commands, prefix the env explicitly:

```bash
adb shell 'XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus espeak-ng "hi"'
```

(An interactive `adb shell` — no command — *does* source `.bashrc`, as does `ssh arduino@host 'espeak…'`.
The host-side `*.py` tools in this repo always prefix this env for exactly this reason.)

---

## 3. The headless fix — disable WirePlumber bluez seat-monitoring (REQUIRED)

**This is the step that makes BT audio work on a headless board.** WirePlumber only starts its
bluez monitor when the logind seat is `"active"`; a headless, lingering session is never `"active"`,
so the monitor (and thus A2DP) never comes up. Disable seat-monitoring for the bluez monitor:

Create `~/.config/wireplumber/wireplumber.conf.d/51-bluez-no-seat.conf`:

```
# Headless board (linger, no active logind seat): don't gate the bluez monitor on an
# active seat, or WirePlumber never starts it and Bluetooth has no A2DP endpoint.
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
```

Restart WirePlumber and verify the adapter now advertises A2DP (the definitive tell):

```bash
systemctl --user restart wireplumber
bluetoothctl show | grep -iE "Audio Source|Audio Sink"
#  -> UUID: Audio Source ...  / UUID: Audio Sink ...
#  (if ABSENT, the monitor still isn't running — re-check this file + restart)
```

**How to recognize this is your problem** (the symptoms before the fix):
- `bluetoothctl connect <MAC>` fails with `org.bluez.Error.Failed br-connection-profile-unavailable`.
- `wpctl status` has **no Bluetooth section**; no `bluez_*` sink in `pactl list sinks short`.
- `bluetoothctl show` lists only `A/V Remote Control` (AVRCP), **no** `Audio Source`/`Audio Sink`.
- WirePlumber's log shows `bluez.lua … startStopMonitor: Seat state changed: lingering` and never
  creates the `api.bluez5.enum.dbus` device (capture with
  `WIREPLUMBER_DEBUG=D wireplumber` while the service is stopped).

### 3b. Stop the sink idle-suspending (fixes clipped/missing speech)

A second WirePlumber drop-in, and the one you want if you're using the board for **voice
output**. WirePlumber suspends an idle node after 5 s (`scripts/node/suspend-node.lua`).
Waking it tears back up the A2DP stream, and that handshake takes long enough to swallow the
beginning of a short utterance — so a two-word announcement can come out clipped, or seem not
to play at all.

The giveaway that fooled us: **some speakers blink their link LED during A2DP stream setup**,
exactly like pairing mode, which looks alarmingly like the Bluetooth connection is dropping.
It isn't — the ACL link stays up the whole time. Only the audio stream is being rebuilt.
Confirm by watching both at once while something plays:

```bash
bluetoothctl devices Connected            # stays connected throughout
pactl list sinks short | grep bluez       # SUSPENDED -> RUNNING is the real event
```

Create `~/.config/wireplumber/wireplumber.conf.d/52-bluez-no-suspend.conf`:

```
monitor.bluez.rules = [
  {
    matches = [
      { node.name = "~bluez_output.*" }
    ]
    actions = {
      update-props = {
        session.suspend-timeout-seconds = 0
      }
    }
  }
]
```

`0` disables the timer outright (the script returns early rather than arming it). Restart
WirePlumber, then confirm the sink settles to `IDLE` and *stays* there instead of dropping to
`SUSPENDED` after five seconds:

```bash
systemctl --user restart wireplumber
pactl list sinks | grep session.suspend-timeout-seconds   # -> "0"
espeak-ng "test"; sleep 10; pactl list sinks short | grep bluez   # -> IDLE, not SUSPENDED
```

Cost: the A2DP stream stays open, so the speaker never idles down on its own. That's the
trade you want for a voice-announcement board, but it will keep a battery speaker awake.

---

## 4. Pairing (one-time per speaker)

Pairing persists across reboots (`/var/lib/bluetooth`), so this is once per speaker.

```bash
bluetoothctl
power on
agent on
default-agent
scan on                        # put the speaker in pairing mode; note its MAC
pair  BC:87:FA:E2:D7:0D
trust BC:87:FA:E2:D7:0D         # 'trust' = auto-accept future reconnects
connect BC:87:FA:E2:D7:0D
quit
```

---

## 5. Daily use

With §2 (`.bashrc`) and §3 (seat fix) in place and the speaker **trusted**, it auto-connects when
powered and in range. To connect/route by hand:

```bash
bluetoothctl connect BC:87:FA:E2:D7:0D
# PipeWire sink name is bluez_output.<MAC>.1  (NOT the old PulseAudio bluez_sink.<MAC>.a2dp_sink)
pactl set-default-sink bluez_output.BC_87_FA_E2_D7_0D.1
espeak-ng "hello"              # or: paplay file.wav, aplay, Piper TTS, ...
```

Native PipeWire equivalents (handy): `wpctl status` (list with IDs), `wpctl set-default <id>`,
`wpctl set-volume <id> 0.8`.

**From your host (over adb):** `bt.py` connects/reports the speaker — `bt.py` (status),
`bt.py connect` (reconnect after an idle disconnect), `bt.py pair`, `bt.py disconnect`.
`volume.py` reports/sets the default sink's volume — `volume.py`, `volume.py 70`,
`volume.py +10`/`-10`, `volume.py mute|toggle`. (`setup-bt-audio.py` calls `bt.py`'s pair flow
for its last step.)

---

## 6. Health check — what "good" looks like

```bash
pactl info | grep "Server Name"      # PulseAudio (on PipeWire <ver>)
bluetoothctl show | grep -iE "Audio Source|Audio Sink"   # both present (A2DP registered)
pactl list sinks short | grep bluez  # bluez_output.<MAC>.1  ...  RUNNING while playing / SUSPENDED idle
wpctl status                         # a Bluetooth device shows under Audio
```

The sink flips to `RUNNING` during playback. At rest you want **`IDLE`** — if it reads
`SUSPENDED`, the no-suspend drop-in (§3b) isn't in effect, and short clips will get clipped.

---

## 7. Troubleshooting (in order of likelihood, headless)

1. **Connects but silent / `br-connection-profile-unavailable` / no `bluez_*` sink** → the
   **seat-monitoring fix (§3)**. By far the #1 headless cause. Confirm with
   `bluetoothctl show | grep Audio` (no Audio Source/Sink = monitor not running).
2. **A fresh shell plays nothing / `pactl: Connection refused`** → env vars not set → put them in
   `~/.bashrc` (§2).
3. **First sound after idle gets swallowed / short clips don't play / the link LED blinks like
   it's re-pairing** → the sink is idle-suspending and the A2DP stream is being rebuilt per
   utterance. **Fix it properly with the no-suspend drop-in (§3b)**; check with
   `pactl list sinks short | grep bluez` (want `IDLE` at rest, not `SUSPENDED`).
   If you can't apply the drop-in, the old workaround was a throwaway wake burst first:
   ```bash
   paplay /usr/share/sounds/alsa/Front_Center.wav 2>/dev/null; sleep 0.5; espeak-ng "the real message"
   ```
   Note a *battery* speaker may still sleep its own amp on idle regardless — that's the
   speaker's own timer, not PipeWire's, and only it can be told not to.
4. **Audio goes to onboard `Headphones` instead of the speaker** → make the bluez sink default:
   `pactl set-default-sink bluez_output.<MAC>.1` (or `wpctl set-default <id>`).
5. **Erratic audio, server identity flip-flops** → both PulseAudio *and* PipeWire installed and
   fighting. Stay on PipeWire; don't start classic `pulseaudio`. Check `pactl info` Server Name.

---

## 8. Why headless is special (background)

Reaching the board over `adb`/ssh with `enable-linger` gives a user session with **no desktop
login**, which breaks two assumptions normal audio setups rely on:

- **No active logind seat** → WirePlumber's seat-gated Bluetooth monitor won't start. This is the
  one that has no obvious error message; it just silently never registers A2DP. → §3.
- **No login session** → `XDG_RUNTIME_DIR` / `DBUS_SESSION_BUS_ADDRESS` aren't set in new shells.
  `enable-linger` keeps the per-user runtime dir and services alive so the sockets exist; you just
  have to point each shell at them. → §2.

Everything else (packages, pairing, routing) is ordinary Linux Bluetooth audio.
