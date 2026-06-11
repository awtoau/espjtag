#!/usr/bin/env python3
"""xtensa_nar.py — bench validation of espjtag.xtensa (the ESP32-S3 XDM access, #9).

Reads the S3 ROM @ 0x40000000 and checks it matches the OpenOCD + probe-rs golden
(xcheck), round-trips a DDR register and a scratch-RAM word (validating both read
and write via instruction injection). All the protocol lives in espjtag/xtensa.py.

Usage:  python3 scripts/xtensa_nar.py --usb 1-1.3.2.2   (or bash scripts/xnar.sh)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util

from espjtag import EspUsbJtag, chips
from espjtag.xtensa import XtensaXDM, DSR, DDR

GOLDEN = [0x1049C500, 0xE52049D5, 0x49F53049, 0x00003400]   # S3 ROM @ 0x40000000
SCRATCH = 0x3FCE0000                                         # S3 DRAM scratch word


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb", required=True)
    args = ap.parse_args()

    j = EspUsbJtag(args.usb)
    x = XtensaXDM(j)
    print(f"IDCODE=0x{j.read_idcode():08x} [{chips.name_for(j.read_idcode()) or '??'}]")
    x.powerup()
    print(f"OCDID=0x{x.nar_read(0x40):08x}  DSR=0x{x.nar_read(DSR):08x}")

    print("\nDDR register round-trip:")
    rt_ok = True
    for pat in (0xA5A5A5A5, 0x5A5A5A5A, 0x00000000, 0xFFFFFFFF):
        x.nar_write(DDR, pat)
        back = x.nar_read(DDR)
        rt_ok = rt_ok and back == pat
        print(f"  0x{pat:08x} -> 0x{back:08x}  {'ok' if back == pat else 'X'}")

    mem_ok = wr_ok = False
    if x.halt():
        print("\nmemory READ @0x40000000 (vs OpenOCD/probe-rs golden):")
        words = x.read_mem(0x40000000, 4)
        mem_ok = words == GOLDEN
        for i, (w, g) in enumerate(zip(words, GOLDEN)):
            print(f"  0x{0x40000000 + i*4:08x} = 0x{w:08x} golden 0x{g:08x} "
                  f"{'ok' if w == g else 'X'}")

        print("\nmemory WRITE round-trip (scratch RAM, saved+restored):")
        saved = x.read_mem(SCRATCH, 1)[0]
        x.write_mem(SCRATCH, [0xDEADBEEF])
        rb = x.read_mem(SCRATCH, 1)[0]
        wr_ok = rb == 0xDEADBEEF
        print(f"  wrote 0xdeadbeef @0x{SCRATCH:08x} -> read 0x{rb:08x}  "
              f"{'ok' if wr_ok else 'X'}")
        x.write_mem(SCRATCH, [saved])                       # restore
        x.resume()
    else:
        print("  FAILED to halt")

    usb.util.dispose_resources(j.dev)
    ok = rt_ok and mem_ok and wr_ok
    print("\n=> RESULT:", "espjtag Xtensa XDM read/write + MEMORY r/w working ✓ "
          "(matches OpenOCD/probe-rs golden)" if ok else "still off")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
