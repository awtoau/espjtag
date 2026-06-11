#!/usr/bin/env python3
"""xcall_test.py — validate XtensaXDM.call_function on the S3 (#29).

Test 1: call an entry that is itself a BREAK — proves the set-PC / RFDO-resume /
BREAK-trap / poll-STOPPED mechanism (the xtensa_start_algorithm port), with no
function and no windowed-ABI question.
Test 2: call spi_flash_attach (a real, windowed ROM func) — tells us whether a
call0-style entry works or we need a call0 bridge.

Usage: python3 scripts/xcall_test.py --usb 1-1.3.1.3.3.3
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
    sram, rom = c["sram"], c["rom"]

    # Test 1 — call a BREAK: validates set-PC/resume/trap (no function involved)
    brk = sram["trap"]
    ret, halted = x.call_function(brk, args=(), stack=sram["stack"], trap=brk)
    print(f"test1 call BREAK@0x{brk:08x}: halted={halted} ret={ret}  "
          f"{'MECHANISM OK' if halted else 'mechanism FAILED'}")

    # Test 2 — call spi_flash_attach(0,0): a real (windowed) ROM function
    ret, halted = x.call_function(rom["spi_flash_attach"], args=(0, 0),
                                  stack=sram["stack"], trap=sram["trap"])
    print(f"test2 call spi_flash_attach: halted={halted} ret={ret}  "
          f"{'ROM CALL OK' if halted else 'did NOT trap (windowed ABI? need bridge)'}")

    # Test 3 — the BETTER WAY: call the ROM's CALL0 entry (_esp_rom_spiflash_read,
    # 0x400009cc) directly — no windowed wrapper, no shim. Pre-poison the buffer so
    # we can tell a real read from stale scratch.
    CALL0_READ = 0x400009CC
    x.write_mem(sram["data"], [0xCCCCCCCC] * 4)
    ret, halted = x.call_function(CALL0_READ, args=(0x0, sram["data"], 16),
                                  stack=sram["stack"], trap=sram["trap"])
    words = x.read_mem(sram["data"], 4)
    magic = words[0] & 0xFF
    print(f"test3 _spiflash_read ret={ret} halted={halted} buf[0]=0x{words[0]:08x} "
          f"magic=0x{magic:02x}  "
          f"{'GATE OK (0xE9) — call0 ROM entry, no shim!' if magic == 0xE9 else '(see below)'}")

    x.resume()
    usb.util.dispose_resources(j.dev)
    return 0


if __name__ == "__main__":
    sys.exit(main())
