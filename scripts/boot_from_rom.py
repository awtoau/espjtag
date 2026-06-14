#!/usr/bin/env python3
"""boot_from_rom.py — boot an ESP32-C6 (or C3/C5/H2) OUT of post-flash ROM
download mode into its app, pinned by stable serial.

For the state where a JTAG/esptool reset left the chip parked in the USB-Serial/
JTAG ROM downloader (PC stuck in ROM, app not running, console silent). A plain
`reset run` (OpenOCD OR espjtag) lands back in ROM 0/3 because it doesn't
re-sample the BOOT strap; the reliable boot is USB-bus-reset (clear the download
latch) + ndmreset + resume — which is exactly what EspUsbJtag.reset_run_from_rom()
does. See debug.py:1130 for the full rationale (proven 3/3 on c6-xiao-b).

Usage:
  python3 scripts/boot_from_rom.py --serial 58:E6:C5:11:B7:EC
  python3 scripts/boot_from_rom.py --usb-path 1-1.3.1.3.4
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from espjtag import EspUsbJtag                                  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="USB serial (MAC), the STABLE id — preferred")
    ap.add_argument("--usb-path", help="bus-port path, e.g. 1-1.3.1.3.4 (volatile)")
    args = ap.parse_args()
    if not args.serial and not args.usb_path:
        ap.error("pass --serial (preferred) or --usb-path")

    j = (EspUsbJtag(serial=args.serial) if args.serial
         else EspUsbJtag(args.usb_path))
    j.examine()
    j.reset_run_from_rom(log=lambda m: print(m))
    print("reset_run_from_rom done — app should be booting "
          "(check the console / monitor).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
