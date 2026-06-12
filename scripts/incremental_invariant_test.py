#!/usr/bin/env python3
"""incremental_invariant_test.py — hardware-free regression test for the bug class
pyOCD's fast_program has (docs/PYOCD-INCREMENTAL-PROOF.md §3): an incremental
engine that erases at one granularity but skips "unchanged" units at a finer one
silently turns the rest of every erased unit into 0xFF.

espjtag.flash_incremental can't desync today (diff = erase = write = 4 KiB, one
`changed` list drives all three), so this test exists to FAIL LOUDLY if a future
change breaks that — e.g. switching to 64 KiB block erase for speed without
re-writing every page of the erased block.

Runs the REAL flash_incremental against a simulated flash (no bench, no USB):
stubs the ROM-call surface, records every erase/write range, then asserts

  [INVARIANT] the union of written ranges covers every erased range exactly —
              no byte inside an erased unit is left unwritten

plus the engine's contract: changed-only programming, identical-image early-out,
the ST-killer (a sum-preserving change must be detected — real CRC, not a sum),
and final simulated-flash == image.

Usage: python3 scripts/incremental_invariant_test.py   (pure host, exit 0 = pass)
"""
import sys
import zlib

sys.path.insert(0, ".")
from espjtag.debug import EspUsbJtag

SEC = 0x1000
NSEC = 16
ERASE_UNITS = {"spiflash_erase_sector": SEC, "spiflash_erase_block": 0x10000}


class SimulatedFlash(EspUsbJtag):
    """The real flash_incremental over a fake chip: ROM calls hit a bytearray."""

    def __init__(self, contents):
        self.flash = bytearray(contents)
        self.staged = {}
        self.erases = []          # (addr, size)
        self.writes = []          # (addr, size)
        self._crc_host_name = "crc32(sim)"

    def _rom_flash_ready(self):
        return True, True, True

    def _chip(self):
        return {"sram": {"data": 0x40800000}, "rom": {"spiflash_config_param": 0}}

    def _crc_host(self):
        return lambda b: zlib.crc32(bytes(b)) & 0xFFFFFFFF

    def flash_crc_region(self, addr, size):
        return zlib.crc32(bytes(self.flash[addr:addr + size])) & 0xFFFFFFFF

    def _flash_crc_many(self, regions):
        return [self.flash_crc_region(a, s) for a, s in regions]

    def write_mem(self, addr, words):
        self.staged[addr] = b"".join(w.to_bytes(4, "little") for w in words)

    def call_rom(self, name, args=()):
        if name == "spiflash_unlock":
            return 0, 0
        if name in ERASE_UNITS:
            unit = ERASE_UNITS[name]
            a = args[0] * unit
            self.flash[a:a + unit] = b"\xFF" * unit
            self.erases.append((a, unit))
            return 0, 0
        if name == "spiflash_write":
            dst, src, n = args
            self.flash[dst:dst + n] = self.staged[src][:n]
            self.writes.append((dst, n))
            return 0, 0
        raise AssertionError(f"unexpected ROM call {name}{args}")


def covered(erases, writes):
    """Every byte of every erased range is inside some written range?"""
    wbytes = set()
    for a, n in writes:
        wbytes.update(range(a, a + n))
    holes = []
    for a, n in erases:
        miss = [b for b in range(a, a + n) if b not in wbytes]
        if miss:
            holes.append((a, n, len(miss)))
    return holes


def main():
    import random
    base = bytearray(random.Random(0xE5B).randbytes(NSEC * SEC))
    base[1 * SEC + 0x100], base[1 * SEC + 0x101] = 0x11, 0x22
    img = bytearray(base)
    img[1 * SEC + 0x100], img[1 * SEC + 0x101] = 0x22, 0x11   # sum-preserving (ST-killer)
    img[5 * SEC + 0x40] ^= 0xA5                               # ordinary change
    assert sum(base[SEC:2 * SEC]) == sum(img[SEC:2 * SEC])
    fails = 0

    def check(name, ok, detail=""):
        nonlocal fails
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
        fails += not ok

    print("[1] incremental B over A (sectors 1=sum-preserving swap, 5=flip changed)")
    j = SimulatedFlash(base)
    res = j.flash_incremental(0, bytes(img), log=None, verify=True)
    holes = covered(j.erases, j.writes)
    check("INVARIANT: written covers every erased byte", not holes,
          f"holes={holes}" if holes else f"{len(j.erases)} erases fully re-written")
    check("changed-only: exactly sectors {1,5} erased",
          sorted(a // SEC for a, _ in j.erases) == [1, 5])
    check("ST-killer: sum-preserving change detected (real CRC)",
          any(a // SEC == 1 for a, _ in j.erases))
    check("result: flash == image", bytes(j.flash) == bytes(img))
    check("verify count matches", res["verified"] == res["written"] == 2)

    print("[2] identical image early-out")
    j2 = SimulatedFlash(img)
    res2 = j2.flash_incremental(0, bytes(img), log=None, verify=True)
    check("zero erases/writes", not j2.erases and not j2.writes and res2["changed"] == 0)

    print(f"VERDICT: {'PASS' if not fails else f'FAIL ({fails})'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
