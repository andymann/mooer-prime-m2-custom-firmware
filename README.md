# Mooer Prime M2 — Custom Firmware: MIDI Clock BPM Sync

Adds **MIDI clock (0xF8) BPM sync** to the Mooer Prime M2 multi-effects
pedal. When a MIDI clock signal arrives on the 3.5 mm TRS MIDI IN jack the
device's BPM engine locks to it automatically — no tapping required.

> **Tested on firmware V100 (file: A0_V100.mr, 221 530 368 bytes).**  
> For a new firmware version see *Re-patching a new firmware version* below.

---

## What it does

Every 24 MIDI clock bytes (= one beat) the patch:

1. Reads the ARM DWT cycle counter to compute the beat period.
2. Converts to BPM (`60 000 / period_ms`).
3. Calls the firmware's own `set_bpm()` function — the display updates and
   all effects respond exactly as if you had tapped the tempo manually.
4. BPM is range-checked to **30–300 BPM** (200–2000 ms per beat); clock
   glitches outside that window are silently ignored.

Non-clock MIDI messages (CC, PC, note on/off …) pass through to the
original handler unchanged.

---

## Files

| File | Purpose |
|---|---|
| `patch_midi_clock.py` | Patches `A0_V100.mr` → `A0_V100_midi_clock.mr` |
| `upload_firmware.py` | Flashes any `.mr` file to the device via USB |
| `requirements.txt` | Python dependencies |
| `discover_offsets.py` | *(see below)* Re-derives all offsets for a new firmware version |

---

## Prerequisites

It is recommended to install dependencies in a virtual environment so they
don't conflict with other Python projects on your system.

```bash
# Create and activate a virtual environment (one-time setup)
python3 -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

After activation your shell prompt shows `(.venv)`.  All `python` and `pip`
commands in this README assume the venv is active.

To deactivate when you're done:

```bash
deactivate
```

For `upload_firmware.py` you also need the native **libhidapi** shared
library:

* **macOS:** `brew install hidapi`
* **Debian/Ubuntu:** `sudo apt install libhidapi-hidraw0`
* **Arch:** `sudo pacman -S hidapi`

**Linux udev rule** (allows non-root HID access):

```
echo 'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="34db", ATTRS{idProduct}=="000d", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/99-mooer-m2.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

---

## Step 1 — Obtain the original firmware

The firmware is embedded in the **Mooer Studio For M2** companion app
installer (Mac: `Mooer Studio For M2_V1.5.0_Setup_Mac.dmg`).

```bash
# macOS
hdiutil attach "Mooer Studio For M2_V1.5.0_Setup_Mac.dmg"
find /Volumes/Mooer* -name "*.mr"
# typically: /Volumes/.../Mooer Studio For M2.app/Contents/Resources/A0_V100.mr
cp "/Volumes/.../A0_V100.mr" ~/A0_V100.mr
hdiutil detach /Volumes/Mooer*
```

Keep the original file **read-only** — never modify it.

```bash
chmod 444 ~/A0_V100.mr
```

---

## Step 2 — Patch the firmware

```bash
python patch_midi_clock.py ~/A0_V100.mr ~/A0_V100_midi_clock.mr
```

Expected output:

```
Source : /Users/you/A0_V100.mr
Output : /Users/you/A0_V100_midi_clock.mr
Patch 1 (hook)       @ file+0x0007c062 : 00f017bd
Patch 2 (trampoline) @ file+0x0007ca94 : 154 bytes written, 138 bytes padding
Changed bytes: 158 at offsets ['0x7c062', '0x7c063', ...]
Done.
```

---

## Step 3 — Flash the firmware

Connect the Mooer Prime M2 via USB.  Make sure Mooer Studio is **not**
running (it will fight over the HID device).

```bash
python upload_firmware.py ~/A0_V100_midi_clock.mr
```

The script will:

1. Open the HID interface and send the reboot command.
2. Wait for the `NO NAME` USB Mass Storage drive to appear.
3. Copy the firmware and write the bootloader trigger file.
4. Eject the drive.

The device flashes and reboots automatically (~30 s).  Do **not**
disconnect USB during flashing.

### Manual upload (fallback)

If the script can't find the drive, do it by hand after step 3 above
prints "device will reboot into mass-storage mode":

```
FIRMWARE/
  A0_V100.mr     ← your patched file
  mooer.log      ← text file containing exactly:  A0_V100.mr
```

Write those two files to the drive's `FIRMWARE/` folder, then eject.

---

## Reverting to stock firmware

Flash the original `A0_V100.mr` the same way:

```bash
python upload_firmware.py ~/A0_V100.mr
```

---

## How the patch works (technical)

The i.MX RT1170 UART interrupt handler passes each MIDI byte to a
dispatcher.  The patch redirects the system-message branch to a 154-byte
trampoline in a zero-padded region of flash.

```
Original flow:
  UART ISR → midi_byte_processor → [cmp r1,#0xF0] → sys_dispatch / channel_handler

Patched flow:
  UART ISR → midi_byte_processor → [cmp r1,#0xF0] → TRAMPOLINE
    if byte ≠ 0xF0 → channel_handler   (unchanged)
    if byte = 0xF0 but ≠ 0xF8 → sys_dispatch   (unchanged)
    if byte = 0xF8:
      read DWT_CYCCNT
      CLK_CTR++
      if CLK_CTR == 24:
        CLK_CTR = 0
        delta_ms = (CYCCNT − PREV_CYCCNT) / 1,000,000
        if 200 ≤ delta_ms ≤ 2000:
          BPM = 60000 / delta_ms
          set_bpm(BPM, 0)
        PREV_CYCCNT = CYCCNT
      else:
        save incremented counter
      return
```

Two 4-byte SRAM variables (CLK_CTR @ `0x2001C120`, PREV_CYCCNT @
`0x2001C124`) live in a zero-initialised region that is confirmed unused by
the original firmware.

### Key addresses (firmware V100)

| Symbol | Address | Notes |
|---|---|---|
| `MIDI_SYSCHECK` | `0x30170FE2` | Original `b.w` overwritten by hook |
| `TRAMPOLINE` | `0x30171A14` | 292-byte zero run, 154 used |
| `set_bpm` | `0x3015F7A8` | `r0`=BPM value, `r1`=0 |
| `CLK_CTR` | `0x2001C120` | Byte, SRAM |
| `PREV_CYCCNT` | `0x2001C124` | uint32, SRAM |
| `DWT_CYCCNT` | `0xE0001004` | ARM Cortex-M7 cycle counter |

---

## Re-patching a new firmware version

When Mooer releases a new firmware, all byte offsets may change.  The
companion discovery prompt below guides an AI session (or a human engineer)
through re-deriving every offset from scratch.

**Input:** the new `Mooer Studio For M2_VX.X.X_Setup_Mac.dmg`  
**Output:** a new patched `.mr` file and updated constants for `patch_midi_clock.py`

### Discovery prompt

> **Task: Patch Mooer Prime M2 firmware for MIDI clock BPM sync (new version)**
>
> You are given: `Mooer Studio For M2_VX.X.X_Setup_Mac.dmg`
>
> **Step 1 — Extract the .mr file**
>
> ```bash
> hdiutil attach "Mooer Studio For M2_VX.X.X_Setup_Mac.dmg"
> find /Volumes/Mooer* -name "*.mr"
> cp <found path> ./A0_VX.mr
> ```
>
> **Step 2 — Parse .mr section headers**
>
> Each section is prefixed by a "Mooer_TAG" marker.  Dump the first 512 bytes
> and find the two sections you need:
>
> - **XIP section**: `load_addr ≈ 0x30100000`
>   → record `XIP_FILE_OFFSET`, `XIP_LOAD_BASE`, `XIP_SIZE`
> - **SRAM section**: `load_addr = 0x20000000`
>   → record `SRAM_FILE_OFFSET`, `SRAM_LOAD_BASE`, `SRAM_SIZE`
>
> The section header layout (offsets from "Mooer_TAG" start):
> `+0x0D` = next section file offset, `+0x11` = data size, `+0x19` = load address.
> Payload starts at `+0x80`.
>
> **Step 3 — Find set_bpm**
>
> Search XIP for `MOVW Rd, #0xEA60` (bytes `4e f6 60 2?`).  Disassemble
> ~40 bytes around the hit with Capstone.  Confirm the function stores r0
> to a struct field **before** the UDIV (r0 = BPM, not period).
> Record `SET_BPM_XIP`.
>
> **Step 4 — Find the MIDI system-message check**
>
> Search XIP for `CMP r1, #0xF0` (bytes `f0 29`).  Disassemble surrounding
> code to find:
> - `MIDI_SYSCHECK_XIP`: address of the `cmp r1, #0xf0`
> - `CHANNEL_MSG_HANDLER_XIP`: target of the "not system" `b.w`
> - `SYS_DISPATCH_XIP`: target of the "is system, not 0xF8" path
>
> **Step 5 — Find trampoline space in XIP**
>
> Find a run of ≥ 200 consecutive zero bytes at an even address in the XIP
> section.  Record `TRAMP_FILE_OFFSET`, `TRAMP_XIP`, `TRAMP_BUDGET`.
>
> **Step 6 — Find SRAM variable space**
>
> Find a zero run of ≥ 16 bytes at an 8-byte-aligned address in the SRAM
> init section.  Assign:
> - `CLK_CTR_SRAM`   = found address (byte)
> - `PREV_CYCCNT_SRAM` = found address + 4 (uint32)
>
> **Step 7 — Assemble the trampoline**
>
> Using Keystone (ARM Thumb-2) assemble the trampoline from the template in
> `patch_midi_clock.py`.  Replace the four NOP placeholder pairs with:
> - `b.w CHANNEL_MSG_HANDLER_XIP` (from_xip = TRAMP + offset of first NOP pair)
> - `b.w SYS_DISPATCH_XIP`
> - `udiv r0, r0, r4`  (divide delta_cycles by 1 000 000)
> - `udiv r0, r4, r0`  (divide 60 000 by delta_ms)
> - `bl SET_BPM_XIP`
>
> UDIV T1 Thumb-2: `HW1 = 0xFBB0|Rn, HW2 = 0xF000|(Rd<<8)|0x00F0|Rm`
> BL T4: same encoding as B.W but `HW2 |= 0xD000` instead of `0x9000`.
>
> **Step 8 — Write the patched file**
>
> - Patch 1: 4 bytes at `xip_to_file(MIDI_SYSCHECK_XIP + 2)` → `encode_bw(…, TRAMP_XIP)`
> - Patch 2: trampoline bytes at `TRAMP_FILE_OFFSET`, zero-padded to `TRAMP_BUDGET`
>
> Update all constants at the top of `patch_midi_clock.py` and run it.
>
> **Step 9 — Verify**
>
> Flash the patched firmware.  Send 60, 100, and 120 BPM MIDI clock.
> The display should show the exact incoming BPM value.
> If it shows the beat period in milliseconds (≈ 500 for 120 BPM), the
> `set_bpm` calling convention has changed — pass `delta_ms` directly and
> skip the `60000/delta_ms` division.

---

## Recovering from a bad flash / manual flashing

tbd

---

## Known limitations

- Non-clock system messages (0xF0–0xFF, excluding 0xF8) are forwarded to
  the original handler.  Other MIDI messages (CC, PC, notes) are unaffected.
- BPM display updates only on the 24th clock pulse; there is one-beat latency
  at song start.
- The DWT cycle counter is enabled by the trampoline on the first 0xF8 byte;
  the very first beat period measurement is discarded (PREV_CYCCNT = 0 guard
  is implicit via the range check).

---

## License

MIT — do with it what you like, no warranty, flash at your own risk.
