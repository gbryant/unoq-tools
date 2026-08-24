# Arduino Uno Q — factory restore (fresh Debian + stock MCU boot)

How to put a board we've modified back to a stock, out-of-the-box state. Companion to
[`unoq-access.md`](https://github.com/gbryant/commander/blob/main/docs/unoq-access.md) (what we changed on the SBC) and
[`zephyr-hal-spike.md`](https://github.com/gbryant/commander/blob/main/docs/zephyr-hal-spike.md) (the option-byte write, in depth).

## The one fact that drives everything: two chips, two domains

The Uno Q is **two independent processors** that do not share storage:

| Chip | Runs | Storage we touched | Reached by |
|---|---|---|---|
| **QRB2210** (Qualcomm MPU) | Debian Linux (the "SBC") | eMMC — OS image, our broker/router systemd changes | `adb`/`ssh` (Debian), or **EDL/qdl** for reflash |
| **STM32U585** (M33 MCU) | commander (Zephyr) | internal flash **option bytes** (`FLASH_OPTR`) — set by `enable-flash-boot` | **SWD only** (on-board OpenOCD via `arduino-debug` over adb) |

> **Does reflashing Debian revert the MCU option bytes? No — it cannot.**
> The fresh-image flash (Arduino Flasher CLI → `qdl`/Firehose over EDL) writes **only the
> QRB2210's eMMC**. EDL/Firehose has no path to the STM32; the STM32's option bytes are in a
> different chip's internal flash, reachable only over SWD. So our `enable-flash-boot` write
> (`FLASH_OPTR 0x1feff8aa → 0x1beff8aa`, `nSWBOOT0=0`/`nBOOT0=1`) **survives any number of
> Debian reflashes**. Reverting it is a separate, manual SWD step — **on us.**

So "back to stock" is **two independent operations**:

1. **SBC / Debian side** — reflash a fresh image (or just undo our systemd changes). A clean
   image automatically restores the stock router/bridge and erases our broker, because our
   changes lived on the eMMC that gets overwritten.
2. **MCU side** — write the option bytes back to the factory value `0x1feff8aa` over SWD.
   The reflash does **not** do this; nothing but an SWD write does.

Either order works (SWD is independent of Debian and of M33 boot mode). Doing the **MCU
revert first**, while your current toolchain/env is known-good, is the safer sequence — then
reflash Debian.

---

## Step 1 — revert the MCU option bytes to factory (SWD, ~1 min)

This is the part the reflash won't do. It restores the stock **BOOT0-pin / ROM-bootloader**
boot mode that the standard Arduino App Lab DFU sketch-flash expects.

**Easiest:** run the revert that ships with any commander-scaffolded Uno Q project
(`cmdr init unoq` writes it) and answer `y` to the boot-byte prompt:
```
cd <your-unoq-project> && ./restore-arduino        # 2nd half writes OPTR back to 0x1feff8aa
```
(`restore-arduino` also disables our broker and unmasks the Arduino router — harmless to run
even if you're about to wipe the eMMC anyway; if you're reflashing in Step 2, only its
boot-byte half matters.)

**Manual equivalent** (what the script does — handy if you're on a fresh stock image, since
`arduino-debug` ships there too):
```bash
adb shell "pkill -f openocd"; adb forward tcp:3333 tcp:3333
( adb shell arduino-debug & ); sleep 6
arm-none-eabi-gdb -batch -ex "target extended-remote localhost:3333" -ex "monitor halt" \
  -ex "monitor stm32l4x unlock 0" \
  -ex "monitor stm32l4x option_write 0 0x40 0x1feff8aa 0x0c000000" \
  -ex "monitor stm32l4x option_load 0" -ex quit
adb shell "pkill -f openocd"; adb forward --remove tcp:3333
```
Notes:
- `0x40` = `FLASH_OPTR`; verify it reads `0x1beff8aa` (our value) before writing.
- The mask `0x0c000000` touches **only bits 26/27** (`nSWBOOT0`/`nBOOT0`) — **never the RDP
  byte** (`0xAA`). RDP is the only true brick risk, and we never go near it.
- **SWD works in any boot mode**, so this is always recoverable — a bad write is fixed by
  reconnecting and rewriting. Run **one** `arduino-debug` at a time (multiple OpenOCD
  instances collide on the SWD and can leave the M33 halted).
- After this, the M33 boots its ROM bootloader, not commander — that **is** the stock state.

> Optional: if you also want the MCU flash blank/stock (not our Zephyr image), erase it over
> the same SWD session (`monitor stm32l4x mass_erase 0`). Not required — the stock App Lab
> flow overwrites the sketch on its next upload anyway.

---

## Step 2 — reflash a fresh Debian image (EDL, ~5–15 min)

This wipes the eMMC and restores the stock OS, stock services (router unmasked, no broker),
and factory data. **Destructive: all data, projects, and config on the board are lost.**

Uses the **Arduino Flasher CLI** (a `qdl`/Firehose front-end), with the board forced into
Qualcomm **EDL** (Emergency Download) mode by a hardware jumper:

1. **Power off** the board (unplug USB).
2. **Short the EDL jumper** on the `JCTL` header — the **two pins furthest from the USB-C
   connector** — with a female-female jumper wire or a shunt. Hold/leave it shorted.
3. **Connect USB** to your computer with the pins still shorted → the board enumerates in EDL
   (you may be prompted to install USB drivers the first time).
4. **Flash** from the extracted Flasher CLI folder:
   ```
   arduino-flasher-cli flash latest          # downloads + flashes the latest stock image
   ```
   Answer `yes` at the prompts; **do not unplug** during the 5–15 min flash. (Under the hood
   this is roughly `qdl --allow-missing --storage emmc prog_firehose_ddr.elf rawprogram0.xml
   patch0.xml`.)
5. On the **success** message, **unplug**, **remove the jumper**, and reconnect USB to boot
   the fresh image normally.

Get the tool from Arduino's software/downloads page. Sources/walk-throughs:
[Arduino Uno Q user manual](https://docs.arduino.cc/tutorials/uno-q/user-manual/),
[Core Electronics reflashing guide](https://core-electronics.com.au/guides/how-to-reinstall-linux-on-the-uno-q-arduino-reflashing-guide/),
[DigiKey: reflash the OS](https://www.digikey.com/en/maker/tutorials/2025/how-to-reflash-the-operating-system-to-your-arduino-uno-q).

### Don't want a full wipe? Undo our SBC changes in place
If the board still boots and you only need to back out *our* modifications (no fresh image),
the SBC half of `restore-arduino` is enough — it disables `commander-broker`, restores
`commander-bridge`, and unmasks the Arduino router stack (`arduino-router.service`,
`-serial.service`, `-serial.path`). See "Revert to stock Arduino" in commander's [unoq-access.md](https://github.com/gbryant/commander/blob/main/docs/unoq-access.md). A
reboot lets the router reclaim `ttyHS1`.

---

## Verify you're back to stock

- **SBC:** `systemctl is-enabled arduino-router.service` → `enabled`;
  `systemctl status commander-broker.service` → not-found/inactive; `/etc/systemd/system`
  has no `*.commander-bak` leftovers and no `/dev/null` masks.
- **MCU:** over SWD, `FLASH_OPTR` reads `0x1feff8aa`; after a power-cycle the M33 sits in the
  ROM bootloader (App Lab can DFU-flash a sketch). To confirm boot mode, read VTOR
  (`0xE000ED08`) — bootloader shows `0x0bf90000`, our flash app showed `0x08000000`.

## Quick reference

| Goal | Action | Touches |
|---|---|---|
| Undo broker/router only (board still boots) | `restore-arduino` (SBC half) | eMMC systemd |
| Fresh Debian image | Arduino Flasher CLI + EDL jumper | eMMC (full wipe) |
| **Revert M33 boot to factory** | `option_write … 0x1feff8aa` over SWD (`restore-arduino` boot-byte half) | **STM32 only — NOT done by reflash** |
