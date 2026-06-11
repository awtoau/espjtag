#!/usr/bin/env python3
"""xflash_test.py — exercise the S3 Xtensa flash API (#29): flash_init (config_param
via the CALL0 bridge), the _rom_flash_ready 0xE9 gate, and that flash_write refuses
safely when the gate fails. On a running app the gate does NOT pass (NULL legacy ROM
globals + deep-nesting attach), so this asserts the SAFE-REFUSAL behaviour, not a
successful program.

Usage: python3 scripts/xflash_test.py --usb 1-1.3.1.3.3.3
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util

from espjtag import EspUsbJtag, chips
from espjtag.xtensa import XtensaXDM


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb", required=True)
    args = ap.parse_args()
    j = EspUsbJtag(args.usb)
    x = XtensaXDM(j)
    c = chips.lookup(j.read_idcode()) or {}
    print(f"IDCODE=0x{j.read_idcode():08x} [{c.get('name','?')}]")
    x.powerup()
    if not x.halt():
        print("FAILED to halt"); return 1
    print("halted.")

    r = x.flash_init()
    print(f"flash_init() -> config_param result {r} ({'OK' if r == 0 else 'non-zero'})")
    ready, rw, magic = x._rom_flash_ready()
    print(f"_rom_flash_ready -> ready={ready} magic=0x{(magic or 0):02x} (want 0xE9)")

    refused = False
    try:
        x.flash_write(0x300000, b"\xde\xad\xbe\xef")
    except RuntimeError as e:
        refused = True
        print(f"flash_write correctly REFUSED: {str(e)[:80]}...")
    ok = (r == 0) and (not ready) and refused
    print("\n=> RESULT:",
          "config_param-via-bridge OK; gate not yet passable (NULL globals/deep attach), "
          "flash_write safely refuses — as designed (#29)" if ok else "unexpected")

    x.resume()
    usb.util.dispose_resources(j.dev)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
