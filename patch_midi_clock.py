#!/usr/bin/env python3
"""
Mooer Prime M2 — MIDI Clock BPM Sync Firmware Patcher
======================================================
Patches firmware A0_V100.mr to sync the device's BPM engine to incoming
MIDI clock (0xF8 bytes, 24 pulses per beat) on the 3.5 mm MIDI IN jack.

Usage
-----
    python patch_midi_clock.py A0_V100.mr [output.mr]

If output is omitted the patched file is written alongside the source as
A0_V100_midi_clock.mr.  The original firmware is NEVER modified.

How the patch works
-------------------
The i.MX RT1170 UART ISR processes one MIDI byte at a time.  The patch
redirects the system-message dispatch branch to a trampoline that:

  1. Lets non-0xF8 bytes fall through to the original handler.
  2. On each 0xF8 byte increments a counter (CLK_CTR) in SRAM.
  3. On the 24th pulse reads the ARM DWT cycle counter, computes the beat
     period in milliseconds, converts to BPM, range-checks [30 … 300 BPM],
     and calls the firmware's own set_bpm(bpm, 0) function.

All addresses and offsets below are hardcoded for firmware version V100
(A0_V100.mr, 221 530 368 bytes).  For a new firmware version run the
companion discovery script (discover_offsets.py) to obtain updated values.

Tested
------
v8.9 — confirmed working on physical Mooer Prime M2 hardware.
60 BPM → display shows 60, 100 BPM → 100, 120 BPM → 120.
"""

import sys
import shutil
import struct
import argparse
from pathlib import Path

# ---------------------------------------------------------------------------
# Firmware-version-specific constants  (V100 / 221 530 368 bytes)
# ---------------------------------------------------------------------------

FIRMWARE_SIZE      = 221_530_368          # exact expected file size (bytes)

# XIP section in the .mr container
XIP_FILE_OFFSET    = 0x0000B080           # byte offset in .mr where XIP data starts
XIP_LOAD_BASE      = 0x30100000           # address XIP section maps to on device
XIP_SIZE           = 0x0017D600           # section byte length

# SRAM init section
SRAM_FILE_OFFSET   = 0x001C5280           # byte offset in .mr where SRAM data starts
SRAM_LOAD_BASE     = 0x20000000

# Patch 1 — hook: redirect the 4-byte B.W at (MIDI_SYSCHECK + 2) to trampoline
HOOK_FILE_OFFSET   = 0x0007C062           # mr file offset of the B.W to overwrite
HOOK_XIP           = 0x30170FE2           # corresponding XIP address (for verification)

# Patch 2 — trampoline destination (292-byte zero run in XIP)
TRAMP_FILE_OFFSET  = 0x0007CA94           # mr file offset of zero run
TRAMP_XIP          = 0x30171A14           # corresponding XIP address
TRAMP_BUDGET       = 292                  # available bytes (zero-filled in original)

# SRAM variables (within the 448-byte zero run at SRAM 0x2001C120)
CLK_CTR_SRAM       = 0x2001C120           # byte: clock-pulse counter (0–23)
PREV_CYCCNT_SRAM   = 0x2001C124           # uint32: previous DWT_CYCCNT snapshot

# Firmware's set_bpm function
SET_BPM_XIP        = 0x3015F7A8           # set_bpm(r0=bpm, r1=ui_flag)

# Addresses the trampoline branches back to
CHANNEL_MSG_HANDLER_XIP = 0x30171008      # non-system-byte path
SYS_DISPATCH_XIP        = 0x30170FE6      # non-clock system-byte path

# ---------------------------------------------------------------------------
# Expected original bytes (safety checks)
# ---------------------------------------------------------------------------

# 4 bytes at HOOK_FILE_OFFSET in the unpatched firmware
ORIGINAL_HOOK_BYTES = bytes([0xFF, 0xF7, 0xF6, 0xBA])  # original B.W encoding

# First 4 bytes of the trampoline zero space must all be 0x00
TRAMP_GUARD = bytes(4)

# ---------------------------------------------------------------------------
# Fully assembled v8.9 trampoline  (154 bytes, verified on device)
# ---------------------------------------------------------------------------
# Disassembly: see README.md / session notes.

V89_TRAMPOLINE = bytes([
    # cmp r1, #0xf0  /  beq is_system  /  b.w CHANNEL_MSG_HANDLER
    0xf0, 0x29, 0x01, 0xd0, 0xff, 0xf7, 0xf6, 0xba,
    # cmp r0, #0xf8  /  beq is_clock   /  b.w SYS_DISPATCH
    0xf8, 0x28, 0x01, 0xd0, 0xff, 0xf7, 0xe1, 0xba,
    # is_clock: push {r4,r5}
    0x30, 0xb4,
    # enable DEMCR.TRCENA (DWT_CYCCNT needs this)
    0x4e, 0xf6, 0xfc, 0x54, 0xce, 0xf2, 0x00, 0x04,  # movw/movt r4,DEMCR
    0x25, 0x68,                                         # ldr  r5,[r4]
    0x45, 0xf0, 0x80, 0x75,                             # orr  r5,r5,#0x1000000
    0x25, 0x60,                                         # str  r5,[r4]
    # enable DWT_CTRL.CYCCNTENA
    0x41, 0xf2, 0x00, 0x04, 0xce, 0xf2, 0x00, 0x04,  # movw/movt r4,DWT_CTRL
    0x25, 0x68,                                         # ldr  r5,[r4]
    0x45, 0xf0, 0x01, 0x05,                             # orr  r5,r5,#1
    0x25, 0x60,                                         # str  r5,[r4]
    # read DWT_CYCCNT into r5
    0x41, 0xf2, 0x04, 0x04, 0xce, 0xf2, 0x00, 0x04,  # movw/movt r4,DWT_CYCCNT
    0x25, 0x68,                                         # ldr  r5,[r4]  ; r5=cycles now
    # increment CLK_CTR
    0x4c, 0xf2, 0x20, 0x14, 0xc2, 0xf2, 0x01, 0x04,  # movw/movt r4,CLK_CTR
    0x20, 0x78,                                         # ldrb r0,[r4]
    0x00, 0xf1, 0x01, 0x00,                             # add.w r0,r0,#1
    0x18, 0x28,                                         # cmp  r0,#24
    0x21, 0xd1,                                         # bne  store_and_ret  (updated v8.9)
    # reset counter
    0x00, 0x20,                                         # movs r0,#0
    0x20, 0x70,                                         # strb r0,[r4]
    # load PREV_CYCCNT, store current, compute delta
    0x4c, 0xf2, 0x24, 0x14, 0xc2, 0xf2, 0x01, 0x04,  # movw/movt r4,PREV_CYCCNT
    0x20, 0x68,                                         # ldr  r0,[r4]  ; r0=prev
    0x25, 0x60,                                         # str  r5,[r4]  ; save current
    0xa5, 0xeb, 0x00, 0x00,                             # sub.w r0,r5,r0 ; delta_cycles
    # divide by 1,000,000 → delta_ms
    0x44, 0xf2, 0x40, 0x24, 0xc0, 0xf2, 0x0f, 0x04,  # movw/movt r4,#1000000
    0xb0, 0xfb, 0xf4, 0xf0,                             # udiv r0,r0,r4
    # range check: delta_ms ∈ [200, 2000]  ↔  BPM ∈ [30, 300]
    0x40, 0xf2, 0xd0, 0x74, 0xc0, 0xf2, 0x00, 0x04,  # movw/movt r4,#2000
    0xa0, 0x42,                                         # cmp  r0,r4
    0x08, 0xd8,                                         # bhi  skip_bpm  (updated v8.9)
    0xc8, 0x28,                                         # cmp  r0,#200
    0x06, 0xd3,                                         # blo  skip_bpm  (updated v8.9)
    # convert period → BPM = 60000 / delta_ms  (new in v8.9)
    0x4e, 0xf6, 0x60, 0x24,                             # movw r4,#0xea60 (60000)
    0xb4, 0xfb, 0xf0, 0xf0,                             # udiv r0,r4,r0
    # call set_bpm(bpm=r0, ui_flag=0)
    0x00, 0x21,                                         # movs r1,#0
    0xed, 0xf7, 0x84, 0xfe,                             # bl   set_bpm  (updated offset)
    # skip_bpm: restore caller frame and return
    0x30, 0xbc,                                         # pop  {r4,r5}
    0x02, 0xb0,                                         # add  sp,#8
    0x80, 0xbd,                                         # pop  {r7,pc}
    # store_and_ret: save incremented counter and return
    0x20, 0x70,                                         # strb r0,[r4]
    0x30, 0xbc,                                         # pop  {r4,r5}
    0x02, 0xb0,                                         # add  sp,#8
    0x80, 0xbd,                                         # pop  {r7,pc}
])

assert len(V89_TRAMPOLINE) == 154, f"trampoline length check failed: {len(V89_TRAMPOLINE)}"


def encode_bw(from_xip: int, to_xip: int) -> bytes:
    """Encode an unconditional 32-bit Thumb-2 B.W instruction."""
    pc = from_xip + 4
    offset = to_xip - pc
    if not (-(1 << 24) <= offset < (1 << 24)):
        raise ValueError(f"B.W offset {offset:#x} out of range")
    enc = offset & 0x1FFFFFF
    enc >>= 1
    S       = (offset >> 24) & 1 if offset < 0 else 0
    imm10H  = (enc >> 11) & 0x3FF
    imm10L  =  enc        & 0x7FF
    I1 = (enc >> 22) & 1;  J1 = (~(I1 ^ S)) & 1
    I2 = (enc >> 21) & 1;  J2 = (~(I2 ^ S)) & 1
    HW1 = 0xF000 | (S << 10) | imm10H
    HW2 = 0x9000 | (J1 << 13) | (J2 << 11) | imm10L
    return struct.pack('<HH', HW1, HW2)


def patch(src: Path, dst: Path) -> None:
    size = src.stat().st_size
    if size != FIRMWARE_SIZE:
        print(f"WARNING: expected {FIRMWARE_SIZE} bytes, got {size}. Proceeding anyway.")

    print(f"Source : {src}")
    print(f"Output : {dst}")

    shutil.copy2(src, dst)

    with open(dst, 'r+b') as f:
        # --- safety: verify hook site looks unpatched ---
        f.seek(HOOK_FILE_OFFSET)
        existing = f.read(4)
        if existing == encode_bw(HOOK_XIP, TRAMP_XIP):
            print("Hook already applied — firmware may already be patched.")
        elif existing != ORIGINAL_HOOK_BYTES:
            print(f"WARNING: unexpected bytes at hook site: {existing.hex()}  (expected {ORIGINAL_HOOK_BYTES.hex()})")

        # --- safety: verify trampoline site is zeroed ---
        f.seek(TRAMP_FILE_OFFSET)
        guard = f.read(4)
        if guard != TRAMP_GUARD:
            print(f"WARNING: trampoline site not zero ({guard.hex()}). Another patch may occupy this space.")

        # --- Patch 1: hook ---
        hook = encode_bw(HOOK_XIP, TRAMP_XIP)
        f.seek(HOOK_FILE_OFFSET)
        f.write(hook)
        print(f"Patch 1 (hook)      @ file+{HOOK_FILE_OFFSET:#010x} : {hook.hex()}")

        # --- Patch 2: trampoline + zero-fill remainder ---
        pad = bytes(TRAMP_BUDGET - len(V89_TRAMPOLINE))
        f.seek(TRAMP_FILE_OFFSET)
        f.write(V89_TRAMPOLINE + pad)
        print(f"Patch 2 (trampoline)@ file+{TRAMP_FILE_OFFSET:#010x} : {len(V89_TRAMPOLINE)} bytes written, {len(pad)} bytes padding")

    # --- verify diff ---
    with open(src, 'rb') as a, open(dst, 'rb') as b:
        orig    = a.read()
        patched = b.read()
    diffs = [i for i in range(len(orig)) if orig[i] != patched[i]]
    print(f"Changed bytes: {len(diffs)} at offsets {[hex(d) for d in diffs[:8]]}")
    print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Patch Mooer Prime M2 firmware with MIDI clock BPM sync.")
    parser.add_argument("firmware", help="Path to original A0_V100.mr")
    parser.add_argument("output", nargs="?", help="Output path (default: <firmware_dir>/A0_V100_midi_clock.mr)")
    args = parser.parse_args()

    src = Path(args.firmware).resolve()
    if not src.exists():
        sys.exit(f"Error: {src} not found")

    if args.output:
        dst = Path(args.output).resolve()
    else:
        dst = src.parent / "A0_V100_midi_clock.mr"

    if dst == src:
        sys.exit("Error: output path must differ from source")

    patch(src, dst)


if __name__ == "__main__":
    main()
