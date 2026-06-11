#!/usr/bin/env python3
"""flash_test.py — validate JTAG flash PROGRAMMING (#30) on a C6, end to end.

Halt, write a test pattern to a spare 4 KiB-aligned sector, read it back, then
restore the sector. flash_write() self-initializes the legacy ROM geometry
(flash_init via esp_rom_spiflash_config_param) when its brick-safety gate fails cold,
then erases + programs via the ROM esp_rom_spiflash_* helpers and verifies via XIP.

Usage:  python3 scripts/flash_test.py --usb 1-1.3.3.3 [--addr 0x300000]
"""
import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util

from espjtag import EspUsbJtag, chips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb", required=True)
    ap.add_argument("--addr", default="0x300000", help="spare 4 KiB-aligned flash offset")
    args = ap.parse_args()
    addr = int(args.addr, 0)

    j = EspUsbJtag(args.usb)
    ic = j.read_idcode()
    print(f"IDCODE=0x{ic:08x} [{chips.name_for(ic) or '??'}]")
    j.examine()
    if not j.halt(disable_wdt=True):
        print("FAILED to halt"); return 1
    print("halted.")

    # bring up the ROM flash read path, then confirm the safety gate (0xE9 magic)
    j.flash_init()
    ready, rw, magic = j._rom_flash_ready()
    print(f"flash_init -> gate ready={ready} (flash[0] magic 0x{(magic or 0):02x}, want 0xE9)")
    if not ready:
        print("ROM flash read path not ready — aborting (mine the init, see #33)")
        j.resume(); usb.util.dispose_resources(j.dev); return 1

    # save the covering 4 KiB sector (raw ROM read) so we can restore it
    saved = j.flash_read_rom(addr, 0x1000 // 4)
    print(f"saved sector @0x{addr:08x} (first word 0x{saved[0]:08x})")

    test = [(0xC0FFEE00 + i) & 0xFFFFFFFF for i in range(64)]    # 256 B pattern
    data = b"".join(struct.pack("<I", w) for w in test)
    print(f"flash_write 0x{addr:08x} +{len(data)} B ...")
    try:
        j.flash_write(addr, data, log=print, verify=True)
    except Exception as e:
        print("flash_write RAISED:", e)
        j.resume(); usb.util.dispose_resources(j.dev); return 1

    back = j.flash_read_rom(addr, len(test))
    ok = back == test
    print(f"read-back via ROM: {'MATCH' if ok else 'MISMATCH'} "
          f"(first 0x{back[0]:08x}, want 0x{test[0]:08x})")

    # restore the original sector (erase + write the saved 4 KiB back)
    orig = b"".join(struct.pack("<I", w) for w in saved)
    try:
        j.flash_write(addr, orig, verify=False)
        print("restored original sector")
    except Exception as e:
        print("restore RAISED (sector left with test pattern — re-flash to recover):", e)

    j.resume()
    usb.util.dispose_resources(j.dev)
    print("\n=> RESULT:",
          "JTAG FLASH PROGRAMMING WORKS (#30) ✓" if ok else "write/verify off")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
