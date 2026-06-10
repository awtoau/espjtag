#!/usr/bin/env python3
"""prove_rom_read.py — STAGE 1b of espjtag #3: prove a real ROM-function call,
read-only (no erase, no program).

Calls the C6 ROM esp_rom_spiflash_read(src=flash_off, dest=scratch_sram, len) to
read a known flash region into SRAM, then compares those words against an
INDEPENDENT read of the same flash bytes through the XIP cache window
(flash_xip + off). If they match, the ROM call works AND the calling convention
(a0/a1/a2, result in a0) is validated with genuine ROM code — without writing
anything to flash.

icache is disabled around the ROM SPI read (the ROM uses SPI1, which shares the
flash bus with the cache; the hart is halted so the app isn't fetching, and we
re-enable + restore everything after). All scratch RAM and clobbered regs are
saved/restored; the app is resumed. Output to tmp/.

Usage: python3 scripts/prove_rom_read.py <usb_path>   (run from the repo root)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util

from espjtag import EspUsbJtag
from espjtag import chips
from espjtag.constants import DMSTATUS

FLASH_OFF = 0x0          # read the image header region (well-known, read-only)
NWORDS = 8               # 32 bytes


def main(path):
    j = EspUsbJtag(path)
    ic = j.read_idcode()
    name = chips.name_for(ic)
    rom = chips.rom_for(ic)
    sram = chips.sram_for(ic)
    xip = (chips.lookup(ic) or {}).get("flash_xip")
    print(f"target: [{name}] IDCODE=0x{ic:08x}")
    if not (rom and sram and xip):
        print("  chip not fully tabled for flash (rom/sram/xip) — aborting")
        return 1
    dest = sram["data"]
    print(f"  ROM esp_rom_spiflash_read=0x{rom['spiflash_read']:08x} "
          f"dest_sram=0x{dest:08x} xip=0x{xip:08x}")

    j.examine()
    if not j.halt():
        print("  FAILED to halt"); return 1
    print("  halted")

    # independent reference: read the same flash bytes via the XIP cache window.
    ref = j.read_mem(xip + FLASH_OFF, NWORDS)
    print(f"  XIP ref @0x{xip + FLASH_OFF:08x}: {[hex(w) for w in ref]}")
    if ref and (ref[0] & 0xFF) != 0xE9:
        print(f"  NOTE: first byte 0x{ref[0] & 0xFF:02x} != 0xE9 (ESP image magic) "
              f"— continuing, comparison still valid")

    # save scratch dest + clobbered state is handled inside call_function for regs;
    # we save/restore the dest SRAM words ourselves.
    saved_dest = j.read_mem(dest, NWORDS)

    # disable icache around the ROM SPI read (shared flash bus), then re-enable.
    cdis = rom["cache_disable_icache"]
    cen = rom["cache_enable_icache"]
    r_dis, h1 = j.call_function(cdis, args=(), stack=sram["stack"], trap=sram["trap"])
    print(f"  Cache_Disable_ICache() -> {None if r_dis is None else hex(r_dis)} halted={h1}")

    # esp_rom_spiflash_read(src=FLASH_OFF, dest=dest_sram, len=NWORDS*4) -> 0=OK
    ret, halted = j.call_function(rom["spiflash_read"],
                                  args=(FLASH_OFF, dest, NWORDS * 4),
                                  stack=sram["stack"], trap=sram["trap"])
    print(f"  esp_rom_spiflash_read(0x{FLASH_OFF:x}, 0x{dest:08x}, {NWORDS*4}) "
          f"-> a0={None if ret is None else hex(ret)} halted={halted}")

    got = j.read_mem(dest, NWORDS)
    print(f"  ROM-read into SRAM : {[hex(w) for w in got]}")

    r_en, h2 = j.call_function(cen, args=(), stack=sram["stack"], trap=sram["trap"])
    print(f"  Cache_Enable_ICache()  -> {None if r_en is None else hex(r_en)} halted={h2}")

    ok = halted and ret == 0 and got == ref
    print(f"  ROM result {ret} (0=OK), bytes match XIP: {got == ref} -> "
          f"{'PASS' if ok else 'FAIL'}")

    # restore scratch dest, resume the app.
    j.write_mem(dest, saved_dest)
    ran = j.resume()
    st = j.dm_read(DMSTATUS)
    print(f"  restored scratch + resume -> {ran}  allrunning={(st >> 11) & 1}")
    usb.util.dispose_resources(j.dev)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
