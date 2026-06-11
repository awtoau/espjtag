#!/usr/bin/env python3
"""xcall_test.py — validate XtensaXDM.call_function on the S3 (#29).

Test 1: call a bare BREAK call0-style (windowed=False) — proves the set-PC /
RFDO-resume / BREAK-trap / poll-STOPPED mechanism.
Test 2: WINDOWED call (default) of esp_rom_spiflash_config_param via the CALL0 bridge.
config_param writes the chip geometry into *rom_spiflash_legacy_data; we point that
(NULL on this app) at a scratch buffer, call config_param windowed, and require the
buffer to hold the geometry we passed AND ret==0. That proves the bridge drives a real
(leaf) windowed ROM function correctly. (Full ROM flash read — the 0xE9 gate — also
needs rom_spiflash_legacy_funcs + the deep-nesting attach; see scripts/flash_test.py.)

Usage: python3 scripts/xcall_test.py --usb 1-1.3.1.3.3.3
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util

from espjtag import EspUsbJtag, chips
from espjtag.xtensa import XtensaXDM

ROM_LEGACY_DATA = 0x3FCEFFE4


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
    sram, rom = c["sram"], c["rom"]

    # Test 1 — call a BREAK call0-style: validates set-PC/resume/trap (no function)
    brk = sram["trap"]
    ret, halted = x.call_function(brk, args=(), stack=sram["stack"], trap=brk,
                                  windowed=False)
    print(f"test1 call0 BREAK@0x{brk:08x}: halted={halted} ret={ret}  "
          f"{'MECHANISM OK' if halted else 'mechanism FAILED'}")

    # Test 2 — windowed config_param via the bridge populates the geometry struct.
    chip_buf = sram["data"] + 0x1400
    x.write_mem(chip_buf, [0] * 8)
    saved_ptr = x.read_mem32(ROM_LEGACY_DATA)
    x.write_mem32(ROM_LEGACY_DATA, chip_buf)
    want = [0, 0x1000000, 0x10000, 0x1000, 0x100, 0xFFFF]
    # warmup: the first resume after halt is occasionally swallowed; one throwaway call
    x.call_function(rom["spiflash_config_param"], args=tuple(want),
                    stack=sram["stack"], trap=sram["trap"])
    ret, halted = x.call_function(rom["spiflash_config_param"], args=tuple(want),
                                  stack=sram["stack"], trap=sram["trap"])
    struct = x.read_mem(chip_buf, 6)
    x.write_mem32(ROM_LEGACY_DATA, saved_ptr)              # restore
    ok = halted and ret == 0 and struct == want
    print(f"test2 windowed config_param: halted={halted} ret={ret} "
          f"struct={[hex(w) for w in struct]}  "
          f"{'WINDOWED ROM CALL OK (bridge drives a real ROM fn)' if ok else 'FAILED'}")

    x.resume()
    usb.util.dispose_resources(j.dev)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
