#!/usr/bin/env python3
"""flash_incremental_test.py — validate espjtag.flash_incremental on a C6/C5: on-chip
CRC-32 diff (ROM crc32_le over a scratch-staged read), write-only-changed, verify.

Includes the ST-killer: image B changes sector 1 normally AND sector 2 with a
SUM-PRESERVING byte swap — the exact change STM32CubeProgrammer's additive-sum
`incremental` silently drops (proven, docs/CUBEPROGRAMMER-BUGS.md). Our CRC-32 must
detect and write it.

Writes to a SAFE flash region (default 0x300000, away from the app).

Usage: .venv/bin/python scripts/flash_incremental_test.py --usb 1-1.3.x.x [--addr 0x300000]
"""
import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util

from espjtag import EspUsbJtag, chips

SEC = 0x1000


def asum(d):
    return sum(d) & 0xFFFFFFFF


def read_region(j, addr, nbytes):
    """Read nbytes of raw flash via per-sector ROM reads (each fits scratch)."""
    out = b""
    for off in range(0, nbytes, SEC):
        words = j.flash_read_rom(addr + off, SEC // 4)
        out += b"".join(int.to_bytes(w, 4, "little") for w in words)
    return out[:nbytes]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb", required=True)
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=0x300000)
    args = ap.parse_args()
    addr = args.addr

    j = EspUsbJtag(args.usb)
    ic = j.read_idcode()
    c = chips.lookup(ic) or {}
    print(f"IDCODE=0x{ic:08x} [{c.get('name','?')}]  test region 0x{addr:08x}")
    j.examine()
    if not j.halt():
        print("halt FAILED"); return 1
    print("halted.")

    # A = 4 sectors of known random data, with a planted pair in sector 2.
    rnd = random.Random(0xC0FFEE)
    A = bytearray(rnd.randbytes(4 * SEC))
    A[2 * SEC + 0x40], A[2 * SEC + 0x41] = 0x11, 0x22
    A = bytes(A)
    # B: sector 1 = real change (sum changes); sector 2 = SWAP (sum preserved!); 0,3 same.
    B = bytearray(A)
    B[1 * SEC + 0x10] ^= 0xFF
    B[2 * SEC + 0x40], B[2 * SEC + 0x41] = 0x22, 0x11
    B = bytes(B)
    s2a, s2b = A[2 * SEC:3 * SEC], B[2 * SEC:3 * SEC]
    print(f"sector-2 additive sum  A=0x{asum(s2a):08x}  B=0x{asum(s2b):08x}  "
          f"-> {'EQUAL — ST would SKIP this real change' if asum(s2a)==asum(s2b) else 'differ'}")

    print("\n[1] cold flash A (writes all that differ):")
    r1 = j.flash_incremental(addr, A, log=print)
    print(f"    {r1}")

    print("[2] re-flash A (must be a no-op — the root early-out):")
    r2 = j.flash_incremental(addr, A, log=print)
    early_ok = r2["changed"] == 0
    print(f"    {r2}  {'EARLY-OUT OK (0 written)' if early_ok else 'FAIL: expected 0 changed'}")

    print("[3] incremental B (must write sectors 1 AND 2 — incl. the sum-preserving one):")
    r3 = j.flash_incremental(addr, B, log=print)
    print(f"    {r3}")

    print("[4] independent verify — raw ROM read-back vs B:")
    got = read_region(j, addr, 4 * SEC)
    full_ok = got == B
    # which sectors actually differ now (should be none)
    diffs = [s for s in range(4) if got[s*SEC:(s+1)*SEC] != B[s*SEC:(s+1)*SEC]]
    print(f"    read-back {'MATCHES B' if full_ok else f'MISMATCH at sectors {diffs}'}")

    sp_caught = r3["changed"] == 2 and full_ok
    wrote2 = "PASS" if r3["changed"] == 2 else f"got {r3['changed']}"
    print("\n--- VERDICT --------------------------------------------------")
    print(f"  early-out (re-flash identical -> 0 writes):      {'PASS' if early_ok else 'FAIL'}")
    print(f"  incremental B wrote exactly sectors 1+2:         {wrote2}")
    print(f"  sum-preserving sector-2 change DETECTED+written: {'PASS' if sp_caught else 'FAIL'}")
    print(f"  => on-chip CRC-32 catches what ST's additive sum silently drops. "
          f"{'CONFIRMED' if (early_ok and sp_caught) else 'CHECK'}")
    print("--------------------------------------------------------------")

    j.resume()
    usb.util.dispose_resources(j.dev)
    return 0 if (early_ok and sp_caught) else 1


if __name__ == "__main__":
    sys.exit(main())
