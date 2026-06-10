#!/usr/bin/env python3
"""dump_chip.py — dump EVERYTHING espjtag can see about a target: identity, JTAG
chain, DTMCS, every RISC-V Debug Module register (decoded), the per-chip table
entry, and — with a brief halt — the CPU-ID CSRs (misa / mvendorid / marchid /
mimpid / mhartid + dpc/dcsr). The metadata snapshot taken BEFORE the formal
cross-tool differential test, so we know exactly what silicon we're comparing.

DM-register + IDCODE/DTMCS reads are read-only and need no halt. The CPU-ID CSRs
are only reachable through an abstract-command register read, which needs the
hart halted — so with --halt we halt, read them, and resume (the live app is
left running). Pass --no-halt to skip the CSR block entirely (fully passive).

Usage:  python3 scripts/dump_chip.py <usb_path> [--no-halt]
        (resolve <usb_path> from the bench config DB, e.g.
         `esp32-zephyr/scripts/dev.py --target c6-maker find`)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util

from espjtag import EspUsbJtag, chips
from espjtag.constants import (
    DMCONTROL, DMSTATUS, HARTINFO, ABSTRACTCS, SBCS,
)

# RISC-V machine CSR numbers (read via abstract-command register access).
CSR = {
    "misa": 0x301, "mvendorid": 0xF11, "marchid": 0xF12,
    "mimpid": 0xF13, "mhartid": 0xF14, "dpc": 0x7B1, "dcsr": 0x7B0,
}


def bits(v, hi, lo):
    return (v >> lo) & ((1 << (hi - lo + 1)) - 1)


def decode_idcode(ic):
    # JTAG IDCODE: version[31:28] part[27:12] mfg[11:1] '1'[0]
    return (f"version={bits(ic,31,28)} part=0x{bits(ic,27,12):04x} "
            f"mfg=0x{bits(ic,11,1):03x}"
            + ("  (Espressif Systems)" if bits(ic, 11, 1) == 0x612 else ""))


def decode_dtmcs(dt):
    return (f"version={bits(dt,3,0)} abits={bits(dt,9,4)} "
            f"dmistat={bits(dt,11,10)} idle={bits(dt,14,12)}")


def decode_dmstatus(s):
    flags = [n for n, b in (
        ("allhalted", 9), ("anyhalted", 8), ("allrunning", 11),
        ("anyrunning", 10), ("authenticated", 7), ("anyhavereset", 18),
        ("allhavereset", 19), ("anyunavail", 12), ("impebreak", 22),
    ) if s & (1 << b)]
    return f"version={bits(s,3,0)}  " + " ".join(flags)


def decode_hartinfo(h):
    return (f"dataaddr=0x{bits(h,11,0):03x} datasize={bits(h,15,12)} "
            f"dataaccess={bits(h,16,16)} nscratch={bits(h,23,20)}")


def decode_abstractcs(a):
    return (f"datacount={bits(a,3,0)} progbufsize={bits(a,28,24)} "
            f"cmderr={bits(a,10,8)} busy={bits(a,12,12)}")


def decode_sbcs(s):
    acc = [w for w, b in (("8", 0), ("16", 1), ("32", 2), ("64", 3), ("128", 4))
           if s & (1 << b)]
    return (f"sbversion={bits(s,31,29)} sbasize={bits(s,11,5)} "
            f"access={'/'.join(acc)}bit sberror={bits(s,14,12)}")


def decode_misa(m):
    mxl = {1: 32, 2: 64, 3: 128}.get(bits(m, 31, 30), "?")
    exts = "".join(c for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
                   if m & (1 << i))
    return f"RV{mxl}  extensions={exts}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("usb_path", nargs="?", default=None)
    ap.add_argument("--no-halt", action="store_true",
                    help="skip the halt-only CPU-ID CSR block (stay passive)")
    args = ap.parse_args()

    j = EspUsbJtag(args.usb_path)
    ic = j.read_idcode()
    name = chips.name_for(ic) or "??"

    print("=" * 70)
    print(f"espjtag chip dump — usb_path={args.usb_path}  [{name}]")
    print("=" * 70)

    print("\n-- JTAG / identity --")
    print(f"  IDCODE        : 0x{ic:08x}   {decode_idcode(ic)}")
    chain = getattr(j, "idcode_chain", None)
    print(f"  scan chain    : {[hex(x) for x in chain] if chain else '(single TAP)'}")
    print(f"  chain layout  : taps_after={j.taps_after} taps_before={j.taps_before} "
          f"idcode_index={j.idcode_index}")
    dt = j.read_dtmcs()
    print(f"  DTMCS         : 0x{dt:08x}   {decode_dtmcs(dt)}")
    print(f"  abits (live)  : {j.abits}   idle-hint={j.idle}")

    print("\n-- RISC-V Debug Module registers (read-only, no halt) --")
    j.examine()
    for nm, addr, dec in (
        ("dmcontrol", DMCONTROL, None),
        ("dmstatus", DMSTATUS, decode_dmstatus),
        ("hartinfo", HARTINFO, decode_hartinfo),
        ("abstractcs", ABSTRACTCS, decode_abstractcs),
        ("sbcs", SBCS, decode_sbcs),
    ):
        v = j.dm_read(addr)
        extra = f"   {dec(v)}" if dec else ""
        print(f"  {nm:11} (0x{addr:02x}): 0x{v:08x}{extra}")

    print("\n-- per-chip table (espjtag.chips) --")
    entry = chips.lookup(ic) or {}
    for k in ("name", "core", "abits", "flash_xip"):
        if k in entry:
            v = entry[k]
            print(f"  {k:11}: {v if not isinstance(v, int) else hex(v)}")
    for k in ("chain", "reset", "rom", "sram", "wdt"):
        if k in entry:
            print(f"  {k:11}: {entry[k]}")

    if not args.no_halt:
        print("\n-- CPU-ID CSRs (brief halt -> read -> resume) --")
        if j.halt():
            for nm, csr in CSR.items():
                v = j.read_register(csr)
                extra = f"   {decode_misa(v)}" if nm == "misa" else ""
                print(f"  {nm:11} (0x{csr:03x}): 0x{v:08x}{extra}")
            ran = j.resume()
            st = j.dm_read(DMSTATUS)
            print(f"  -> resumed={ran}  allrunning={(st >> 11) & 1}")
        else:
            print("  FAILED to halt — skipping CSR block (app left running)")

    usb.util.dispose_resources(j.dev)
    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
