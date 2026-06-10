#!/usr/bin/env python3
"""check_flash_gate.py — verify the flash_write SAFETY GATE (#3), read-only.

Confirms _rom_flash_ready() correctly reports the running app's ROM spiflash
state is NOT configured, and that flash_write() therefore REFUSES (raises) without
erasing or programming anything. No flash writes occur. Restores scratch + resumes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util

from espjtag import EspUsbJtag
from espjtag import chips
from espjtag.constants import DMSTATUS


def main(path):
    j = EspUsbJtag(path)
    ic = j.read_idcode()
    print(f"[{chips.name_for(ic)}] IDCODE=0x{ic:08x}")
    j.examine()
    if not j.halt():
        print("FAILED to halt"); return 1

    ready, rw, xw = j._rom_flash_ready()
    print(f"  _rom_flash_ready -> {ready}")
    print(f"    ROM-read words: {None if rw is None else [hex(w) for w in rw]}")
    print(f"    XIP    words:   {None if xw is None else [hex(w) for w in xw]}")

    # flash_write to a high scratch offset MUST refuse (gate fails) — nothing erased.
    refused = False
    try:
        j.flash_write(0x3F0000, b"\xaa\xbb\xcc\xdd", verify=False)
    except RuntimeError as e:
        refused = True
        print(f"  flash_write correctly REFUSED: {str(e)[:80]}...")
    print(f"  gate result: ready={ready} write_refused={refused} -> "
          f"{'PASS (safe)' if (not ready and refused) else 'UNEXPECTED'}")

    j.resume()
    st = j.dm_read(DMSTATUS)
    print(f"  resumed allrunning={(st >> 11) & 1}")
    usb.util.dispose_resources(j.dev)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
