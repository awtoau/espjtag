#!/usr/bin/env python3
"""probe_flash_map.py — read-only investigation (#3): figure out what the XIP
window maps and whether the ROM esp_rom_spiflash_read returns sane flash data.

For a set of flash offsets, read N words via the ROM read (into scratch SRAM) and,
separately, read the XIP window at (xip + off). Print both so we can see the
mapping and whether the ROM read is configured. Disables icache around the ROM
reads. No writes to flash. Restores scratch + resumes the app. Output to tmp/.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util

from espjtag import EspUsbJtag
from espjtag import chips
from espjtag.constants import DMSTATUS

OFFSETS = [0x0, 0x1000, 0x8000, 0x10000, 0x20000, 0x30000]
NW = 4


def hexw(ws):
    return " ".join(f"{w:08x}" for w in ws)


def main(path):
    j = EspUsbJtag(path)
    ic = j.read_idcode()
    rom = chips.rom_for(ic)
    sram = chips.sram_for(ic)
    xip = (chips.lookup(ic) or {}).get("flash_xip")
    dest = sram["data"]
    j.examine()
    if not j.halt():
        print("FAILED to halt"); return 1
    print(f"[{chips.name_for(ic)}] xip=0x{xip:08x} romread=0x{rom['spiflash_read']:08x}")

    saved_dest = j.read_mem(dest, NW)
    # status via ROM
    r_dis, _ = j.call_function(rom["cache_disable_icache"], args=(),
                               stack=sram["stack"], trap=sram["trap"])
    print(f"  Cache_Disable_ICache -> {r_dis}")
    print(f"  {'offset':>8}  {'ROM read (flash)':<35}  XIP window")
    for off in OFFSETS:
        ret, halted = j.call_function(rom["spiflash_read"], args=(off, dest, NW * 4),
                                      stack=sram["stack"], trap=sram["trap"])
        romw = j.read_mem(dest, NW)
        # re-enable cache to read XIP correctly, then disable again for next ROM read
        j.call_function(rom["cache_enable_icache"], args=(),
                        stack=sram["stack"], trap=sram["trap"])
        xipw = j.read_mem(xip + off, NW)
        j.call_function(rom["cache_disable_icache"], args=(),
                        stack=sram["stack"], trap=sram["trap"])
        print(f"  {off:08x}  a0={ret} {hexw(romw):<31}  {hexw(xipw)}")
    j.call_function(rom["cache_enable_icache"], args=(),
                    stack=sram["stack"], trap=sram["trap"])

    j.write_mem(dest, saved_dest)
    j.resume()
    st = j.dm_read(DMSTATUS)
    print(f"  resumed allrunning={(st >> 11) & 1}")
    usb.util.dispose_resources(j.dev)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
