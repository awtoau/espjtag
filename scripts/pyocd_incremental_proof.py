#!/usr/bin/env python3
"""pyocd_incremental_proof.py — prove (on real silicon) that pyOCD's incremental
flash (`smart_flash` + `fast_program`, on-target CRC-32) CATCHES the exact
sum-preserving change that STM32CubeProgrammer's `incremental` silently skips
(see st_incremental_proof.py), while still SKIPPING genuinely unchanged sectors.

Same differential design, same STM32F427, same images (seed 0x1234):
  sector 1 = adjacent-byte SWAP   (content differs, additive byte-sum identical)
  sector 2 = single-byte FLIP     (sum-changing control)
  sectors 0+3 = unchanged         (must be SKIPPED, proving it's incremental,
                                   not a trivially-correct full rewrite)

INDEPENDENT VERIFICATION: setup (write A) and all read-backs use the open texane
`st-flash` (BSD). pyOCD performs ONLY the incremental download under test, so the
result is never self-certified. Target override stm32f429xi: F42x/F43x share
DEV_ID 0x419 and the identical flash sector map; only first 64 KB is touched.

PASS = sector 1 reads back as B (CRC caught the sum-preserving swap) AND
sector 2 as B AND pyOCD's log shows >0 skipped bytes (sectors 0+3 untouched).

Usage: python3 scripts/pyocd_incremental_proof.py --sn 004D00373033510135393935
"""
import argparse
import logging
import os
import random
import re
import subprocess
import sys

BASE = 0x08000000
SIZE = 0x10000           # 64 KB test window = sectors 0..3
SEC = 0x4000             # 16 KB sector
SUMPRES_OFF = 0x4100     # sector 1: adjacent-byte swap (sum-preserving)
CTRL_OFF = 0x8100        # sector 2: single-byte flip (sum-changing control)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(ROOT, "tmp")


def st(sn, *args):
    """texane st-flash (open/BSD) — independent setup + verification reader/writer."""
    r = subprocess.run(["st-flash", "--serial", sn] + list(args),
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def st_write(sn, path):
    return st(sn, "--reset", "write", path, hex(BASE))


def st_read(sn, path, size=SIZE):
    return st(sn, "read", path, hex(BASE), hex(size))


def ssum(buf, sec):
    return sum(buf[sec * SEC:(sec + 1) * SEC]) & 0xFFFFFFFF


def make_images():
    """Byte-identical to st_incremental_proof.make_images (same seed)."""
    a = bytearray(random.Random(0x1234).randbytes(SIZE))
    a[SUMPRES_OFF], a[SUMPRES_OFF + 1] = 0x11, 0x22   # sector-1 pair
    a[CTRL_OFF] = 0x00                                # sector-2 control
    b = bytearray(a)
    b[SUMPRES_OFF], b[SUMPRES_OFF + 1] = 0x22, 0x11   # swap -> sum preserved
    b[CTRL_OFF] = 0xFF                                # flip -> sum +0xFF
    return bytes(a), bytes(b)


def lp(name):
    return os.path.join(TMP, name)


def pyocd_incremental_flash(sn, path, log_path, fast_program):
    """The tool under test: pyOCD smart_flash (+ optional fast_program CRC-32)."""
    handler = logging.FileHandler(log_path, mode="w")
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    try:
        from pyocd.core.helpers import ConnectHelper
        from pyocd.flash.file_programmer import FileProgrammer
        with ConnectHelper.session_with_chosen_probe(
                unique_id=sn,
                options={"target_override": "stm32f429xi",
                         "frequency": 8_000_000,
                         "smart_flash": True,
                         "fast_program": fast_program,  # True = on-target CRC-32 analysis
                         "chip_erase": "sector"}) as session:
            FileProgrammer(session).program(path, base_address=BASE)
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    return open(log_path).read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sn", required=True)
    ap.add_argument("--no-fast", action="store_true",
                    help="fast_program=False: default pyOCD mode (host read-back compare)")
    args = ap.parse_args()
    os.makedirs(TMP, exist_ok=True)
    sn = args.sn

    A, B = make_images()
    open(lp("pyp_A.bin"), "wb").write(A)
    open(lp("pyp_B.bin"), "wb").write(B)

    print(f"sector-1 sum  A=0x{ssum(A,1):08x}  B=0x{ssum(B,1):08x}  "
          f"-> {'EQUAL (sum-preserving)' if ssum(A,1)==ssum(B,1) else 'DIFFER'}")
    print(f"sector-2 sum  A=0x{ssum(A,2):08x}  B=0x{ssum(B,2):08x}  "
          f"-> {'DIFFER (control)' if ssum(A,2)!=ssum(B,2) else 'EQUAL'}")
    assert ssum(A, 1) == ssum(B, 1) and ssum(A, 2) != ssum(B, 2)
    assert A[SEC:2 * SEC] != B[SEC:2 * SEC]

    print(f"\n[1] (st-flash, independent) writing base image A to {sn} ...")
    rc, out = st_write(sn, lp("pyp_A.bin"))
    if rc != 0:
        print(f"  st-flash write A FAILED rc={rc}"); sys.stdout.write(out[-500:]); return 1
    st_read(sn, lp("pyp_rd.bin"))
    if open(lp("pyp_rd.bin"), "rb").read() != A:
        print("  A did NOT verify (st-flash read) — aborting (premise broken)."); return 1
    print("  A written + verified by st-flash (pyOCD not yet involved).")

    mode = "fast_program=False (read-back compare)" if args.no_fast else "fast_program=True (CRC-32)"
    print(f"[2] (pyOCD) incremental-flashing B (smart_flash, {mode}) ...")
    log = pyocd_incremental_flash(sn, lp("pyp_B.bin"), lp("pyp_incr.log"),
                                  fast_program=not args.no_fast)
    m = re.search(r"[Ee]rased (\d+) bytes.*programmed (\d+) bytes.*skipped (\d+) bytes", log)
    if m:
        erased, programmed, skipped = (int(g) for g in m.groups())
        print(f"  pyOCD stats: erased={erased} programmed={programmed} skipped={skipped}")
    else:
        erased = programmed = skipped = None
        print("  (no erase/program/skip stats line found — see tmp/pyp_incr.log)")

    print("    (st-flash, independent) reading back ...")
    st_read(sn, lp("pyp_rd.bin"))
    R = open(lp("pyp_rd.bin"), "rb").read()

    s1B, s2B = B[SEC:2*SEC], B[2*SEC:3*SEC]
    sec1_written = R[SEC:2*SEC] == s1B
    sec2_written = R[2*SEC:3*SEC] == s2B
    others_intact = R[:SEC] == B[:SEC] and R[3*SEC:4*SEC] == B[3*SEC:4*SEC]
    print("\n--- RESULT (read back by st-flash, not pyOCD) -----------------")
    print(f"  sector-1 (sum-preserving swap): byte@0x{SUMPRES_OFF:x} = 0x{R[SUMPRES_OFF]:02x} "
          f"(A=0x{A[SUMPRES_OFF]:02x} B=0x{B[SUMPRES_OFF]:02x})  "
          f"-> {'WRITTEN (==B) — CRC caught it' if sec1_written else 'SKIPPED (==A) — MISSED'}")
    print(f"  sector-2 (control flip):        byte@0x{CTRL_OFF:x} = 0x{R[CTRL_OFF]:02x} "
          f"(A=0x{A[CTRL_OFF]:02x} B=0x{B[CTRL_OFF]:02x})  "
          f"-> {'WRITTEN (==B)' if sec2_written else 'not written (==A)'}")
    print(f"  sectors 0+3 (unchanged):        content correct: {others_intact}; "
          f"pyOCD skipped {skipped} bytes" + (" (>0 => genuinely incremental)"
          if skipped else " (skip not confirmed from log)"))
    if sec1_written and sec2_written and others_intact and (skipped or 0) > 0:
        print("  VERDICT: PASS — pyOCD wrote BOTH changed sectors (incl. the")
        print("           sum-preserving swap CubeProgrammer drops) and skipped the")
        print("           unchanged ones. Correct incremental flash, same probe,")
        print(f"           same chip, pure Python.  [{mode}]")
        verdict = 0
    elif sec1_written and sec2_written and others_intact:
        print("  VERDICT: CORRECT but skip unproven from log — content right; check")
        print("           tmp/pyp_incr.log for the analyze/skip stats."); verdict = 2
    else:
        print("  VERDICT: FAIL — see above + tmp/pyp_incr.log."); verdict = 1
    print("--------------------------------------------------------------")
    return verdict


if __name__ == "__main__":
    sys.exit(main())
