#!/usr/bin/env python3
"""
Mooer Prime M2 — USB Firmware Uploader
=======================================
Flashes a custom (patched) .mr firmware file to the Mooer Prime M2 without
needing the official companion app.

Protocol (reverse-engineered from Mooer Studio For M2 v1.5.0 app.asar):
  1. Find the HID device (VID=0x34DB, PID=0x000D).
  2. Send CMD 193 → device reboots into USB Mass Storage mode.
  3. Wait for the "NO NAME" USB drive to mount.
  4. Copy the firmware .mr file to  FIRMWARE/A0_V100.mr  on the drive.
  5. Write  FIRMWARE/mooer.log  containing the firmware filename (no newline).
  6. Eject the drive → bootloader verifies and flashes; device reboots.

Usage
-----
    python upload_firmware.py <firmware.mr> [--drive /Volumes/NO_NAME]

On macOS the drive usually appears as /Volumes/NO\ NAME.
On Linux it depends on your automount configuration (check `lsblk` or
`/media/$USER/`).  Pass --drive to override the auto-detect.

Dependencies
------------
    pip install hidapi

The hidapi package wraps the native libhidapi.  On macOS install it with
Homebrew first:  brew install hidapi
On Linux:        sudo apt install libhidapi-hidraw0  (or -libusb variant)

CAUTION
-------
Flashing incorrect firmware can brick the device.  Always keep a copy of
the original A0_V100.mr.  The authors accept no liability for damage.
"""

import sys
import os
import time
import shutil
import argparse
import platform
from pathlib import Path

# ---------------------------------------------------------------------------
# HID constants (Mooer Prime M2)
# ---------------------------------------------------------------------------
VID = 0x34DB
PID = 0x000D

CMD_REBOOT_TO_MSC = 193          # reboots device into USB Mass Storage mode

PACKET_START      = bytes([0xAA, 0x55])
REPORT_ID         = 0x00          # HID report ID (varies; 0 if no report ID used)
HID_PACKET_SIZE   = 64            # bytes per HID report


def crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, final XOR 0xFFFF."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc ^ 0xFFFF


def build_packet(cmd: int, data: bytes = b'') -> bytes:
    """Build a framed HID packet: [AA 55 len_lo len_hi cmd data… crc_hi crc_lo]."""
    payload = bytes([cmd]) + data
    length  = len(payload)
    header  = PACKET_START + bytes([length & 0xFF, (length >> 8) & 0xFF])
    body    = header + payload
    crc     = crc16_ccitt(body)
    return body + bytes([crc >> 8, crc & 0xFF])


def send_reboot_to_msc(device) -> None:
    """Send CMD 193 to reboot the device into USB Mass Storage mode."""
    packet = build_packet(CMD_REBOOT_TO_MSC)
    # Pad to HID report size, prepend report ID byte
    padded = bytes([REPORT_ID]) + packet + bytes(HID_PACKET_SIZE - len(packet))
    device.write(padded[:HID_PACKET_SIZE + 1])
    print("  → CMD 193 sent; device will reboot into mass-storage mode …")


def find_drive(label: str = "NO NAME", timeout: int = 30) -> Path:
    """Wait for the USB mass-storage drive to appear and return its mount path."""
    system = platform.system()
    print(f"Waiting for '{label}' drive to mount (up to {timeout}s) …")
    for _ in range(timeout):
        if system == "Darwin":
            candidate = Path(f"/Volumes/{label}")
            if candidate.is_dir():
                return candidate
            # macOS sometimes uses underscores
            candidate2 = Path("/Volumes/NO_NAME")
            if candidate2.is_dir():
                return candidate2
        elif system == "Linux":
            # Check common automount locations
            for base in [Path(f"/media/{os.getenv('USER','user')}"),
                         Path("/media"), Path("/mnt"), Path("/run/media")]:
                for child in base.glob("**/NO?NAME") if base.exists() else []:
                    if child.is_dir():
                        return child
                for child in base.glob("**/NO_NAME") if base.exists() else []:
                    if child.is_dir():
                        return child
        time.sleep(1)
    raise RuntimeError(
        f"Drive '{label}' did not appear within {timeout}s.\n"
        "Check that the device rebooted and is mounted.  Pass --drive to specify the path manually."
    )


def eject_drive(drive: Path) -> None:
    """Eject / unmount the USB drive."""
    system = platform.system()
    print(f"Ejecting {drive} …")
    if system == "Darwin":
        os.system(f'diskutil eject "{drive}"')
    elif system == "Linux":
        # Try umount; may need sudo
        ret = os.system(f'umount "{drive}"')
        if ret != 0:
            print("  umount failed; try:  sudo umount " + str(drive))
    else:
        print("  Auto-eject not supported on this OS.  Eject the drive manually.")


def upload(firmware_path: Path, drive_override: str | None = None) -> None:
    try:
        import hid
    except ImportError:
        sys.exit(
            "Error: 'hid' package not found.\n"
            "Install it:  pip install hidapi\n"
            "macOS also needs:  brew install hidapi"
        )

    # -----------------------------------------------------------------------
    # Step 1: open HID device
    # -----------------------------------------------------------------------
    print(f"Looking for Mooer Prime M2  (VID={VID:#06x}, PID={PID:#06x}) …")
    try:
        dev = hid.device()
        dev.open(VID, PID)
        dev.set_nonblocking(False)
        info = dev.get_manufacturer_string()
        print(f"  Found: {info}")
    except OSError as e:
        sys.exit(
            f"HID open failed: {e}\n"
            "Is the Mooer Prime M2 connected and powered on?\n"
            "On Linux you may need a udev rule — see README.md."
        )

    # -----------------------------------------------------------------------
    # Step 2: reboot device into mass-storage mode
    # -----------------------------------------------------------------------
    send_reboot_to_msc(dev)
    dev.close()
    time.sleep(2)   # give the device a moment to disconnect

    # -----------------------------------------------------------------------
    # Step 3: wait for drive
    # -----------------------------------------------------------------------
    if drive_override:
        drive = Path(drive_override)
        if not drive.is_dir():
            sys.exit(f"Error: --drive path '{drive}' does not exist or is not mounted yet.")
    else:
        drive = find_drive()
    print(f"  Drive found: {drive}")

    # -----------------------------------------------------------------------
    # Step 4: copy firmware
    # -----------------------------------------------------------------------
    fw_dir = drive / "FIRMWARE"
    fw_dir.mkdir(exist_ok=True)

    dest_fw = fw_dir / "A0_V100.mr"
    print(f"Copying {firmware_path.name} → {dest_fw} ({firmware_path.stat().st_size / 1_048_576:.1f} MB) …")
    shutil.copy2(firmware_path, dest_fw)
    print("  Copy complete.")

    # -----------------------------------------------------------------------
    # Step 5: write bootloader trigger
    # -----------------------------------------------------------------------
    log_path = fw_dir / "mooer.log"
    log_path.write_text("A0_V100.mr")   # no newline, exact filename
    print(f"Wrote trigger file: {log_path}")

    # -----------------------------------------------------------------------
    # Step 6: sync and eject
    # -----------------------------------------------------------------------
    try:
        import subprocess
        subprocess.run(["sync"], check=False)
    except Exception:
        pass

    eject_drive(drive)
    print("\nFlashing in progress.  The device will reboot automatically when done.")
    print("Do NOT disconnect the USB cable until the device has fully restarted.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Flash a custom .mr firmware to the Mooer Prime M2 via USB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("firmware", help="Path to the .mr firmware file to flash")
    parser.add_argument(
        "--drive",
        metavar="PATH",
        help="Mount path of the 'NO NAME' USB drive (auto-detected if omitted)",
    )
    args = parser.parse_args()

    fw = Path(args.firmware).resolve()
    if not fw.exists():
        sys.exit(f"Error: firmware file not found: {fw}")
    if fw.suffix.lower() != ".mr":
        print(f"WARNING: file extension is '{fw.suffix}', expected '.mr'")

    upload(fw, args.drive)


if __name__ == "__main__":
    main()
