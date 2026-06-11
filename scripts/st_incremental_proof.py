#!/usr/bin/env python3
"""st_incremental_proof.py — prove (on real silicon) that STM32CubeProgrammer's
`incremental` download decides "sector unchanged?" with a WEAK 32-bit additive
byte-sum, by a sum-preserving differential flash on an STM32F427.

Method (black-box — cites nothing decompiled), NO backup (test boards):
  1. Flash base image A (first 64 KB = four 16 KB sectors).
  2. `incremental`-flash B, which differs from A in two sectors:
       - sector 1: an adjacent-byte SWAP  -> content changes, additive sum does NOT.
       - sector 2: a single-byte FLIP     -> sum changes (the control).
  3. Read back and analyze.

Verdict:
  sector-1 reads back as A (swap dropped) AND sector-2 as B (control written)
     -> the diff-checksum is ADDITIVE: a sum-preserving change was silently skipped.
  sector-1 reads back as B -> it caught the swap -> real CRC/compare (finding wrong).
  sector-2 reads back as A -> `incremental` never engaged -> inconclusive (see log).

A sector-1 == A result cannot be explained by a full/legacy write (writes everything
-> sector-1 == B) nor by a CRC (detects the change -> sector-1 == B). Only an
additive-sum collision yields a skip. The control proves incremental actually ran.

Usage: python3 scripts/st_incremental_proof.py --sn 004D00373033510135393935
"""
import argparse
import os
import random
import subprocess
import sys

CLI = "/home/dan/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI"
BASE = 0x08000000
SIZE = 0x10000           # 64 KB test window = sectors 0..3
SEC = 0x4000             # 16 KB sector
SUMPRES_OFF = 0x4100     # sector 1: adjacent-byte swap (sum-preserving)
CTRL_OFF = 0x8100        # sector 2: single-byte flip (sum-changing control)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(ROOT, "tmp")


def cube(sn, *args, log=None):
    cmd = [CLI, "-c", "port=SWD", f"sn={sn}", "freq=8000", "mode=UR"] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = r.stdout + r.stderr
    if log:
        with open(log, "w") as f:
            f.write("$ " + " ".join(cmd) + "\n\n" + out)
    return r.returncode, out


def ssum(buf, sec):
    return sum(buf[sec * SEC:(sec + 1) * SEC]) & 0xFFFFFFFF


def make_images():
    a = bytearray(random.Random(0x1234).randbytes(SIZE))
    a[SUMPRES_OFF], a[SUMPRES_OFF + 1] = 0x11, 0x22   # sector-1 pair
    a[CTRL_OFF] = 0x00                                # sector-2 control
    b = bytearray(a)
    b[SUMPRES_OFF], b[SUMPRES_OFF + 1] = 0x22, 0x11   # swap -> sum preserved
    b[CTRL_OFF] = 0xFF                                # flip -> sum +0xFF
    return bytes(a), bytes(b)


def lp(name):
    return os.path.join(TMP, name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sn", required=True)
    args = ap.parse_args()
    os.makedirs(TMP, exist_ok=True)
    sn = args.sn

    A, B = make_images()
    open(lp("stp_A.bin"), "wb").write(A)
    open(lp("stp_B.bin"), "wb").write(B)

    print(f"sector-1 sum  A=0x{ssum(A,1):08x}  B=0x{ssum(B,1):08x}  "
          f"-> {'EQUAL (sum-preserving)' if ssum(A,1)==ssum(B,1) else 'DIFFER'}")
    print(f"sector-2 sum  A=0x{ssum(A,2):08x}  B=0x{ssum(B,2):08x}  "
          f"-> {'DIFFER (control)' if ssum(A,2)!=ssum(B,2) else 'EQUAL'}")
    assert ssum(A, 1) == ssum(B, 1), "sector-1 must be sum-preserving"
    assert ssum(A, 2) != ssum(B, 2), "sector-2 sum must differ"
    assert A[SEC:2 * SEC] != B[SEC:2 * SEC], "sector-1 content must differ"

    print(f"\n[1] flashing base image A (full/legacy) to {sn} ...")
    rc, out = cube(sn, "-d", lp("stp_A.bin"), hex(BASE), log=lp("stp_flashA.log"))
    if rc != 0:
        print(f"  flash A FAILED rc={rc}"); sys.stdout.write(out[-500:]); return 1
    cube(sn, "-u", hex(BASE), hex(SIZE), lp("stp_rd.bin"))
    if open(lp("stp_rd.bin"), "rb").read() != A:
        print("  A did NOT verify on-chip — aborting (premise broken).")
        return 1
    print("  A verified on-chip.")

    print("[2] INCREMENTAL-flashing B ...")
    rc, out = cube(sn, "-d", lp("stp_B.bin"), hex(BASE), "incremental", log=lp("stp_incr.log"))
    low = out.lower()
    engaged = any(k in low for k in ("modified sector", "checksum verify"))
    fellback = "legacy" in low and ("not" in low or "instead" in low)
    print(f"  rc={rc}  incremental-engaged~{engaged}  legacy-fallback~{fellback}")

    cube(sn, "-u", hex(BASE), hex(SIZE), lp("stp_rd.bin"))
    R = open(lp("stp_rd.bin"), "rb").read()

    s1A, s1B, s1R = A[SEC:2*SEC], B[SEC:2*SEC], R[SEC:2*SEC]
    s2A, s2B, s2R = A[2*SEC:3*SEC], B[2*SEC:3*SEC], R[2*SEC:3*SEC]
    sec1_skipped = s1R == s1A and s1R != s1B
    sec1_written = s1R == s1B
    sec2_written = s2R == s2B
    print("\n--- RESULT ---------------------------------------------------")
    print(f"  sector-1 (sum-preserving swap): "
          f"byte@0x{SUMPRES_OFF:x} = 0x{R[SUMPRES_OFF]:02x} "
          f"(A=0x{A[SUMPRES_OFF]:02x} B=0x{B[SUMPRES_OFF]:02x})  "
          f"-> {'SKIPPED (==A)' if sec1_skipped else 'written (==B)' if sec1_written else 'OTHER'}")
    print(f"  sector-2 (control flip):        "
          f"byte@0x{CTRL_OFF:x} = 0x{R[CTRL_OFF]:02x} "
          f"(A=0x{A[CTRL_OFF]:02x} B=0x{B[CTRL_OFF]:02x})  "
          f"-> {'WRITTEN (==B)' if sec2_written else 'not written (==A)'}")
    if sec2_written and sec1_skipped:
        print("  VERDICT: ADDITIVE SUM CONFIRMED — a sum-preserving change was")
        print("           silently SKIPPED while a real change was written.")
        verdict = 0
    elif sec2_written and sec1_written:
        print("  VERDICT: NOT additive — it caught the sum-preserving change (real CRC).")
        verdict = 1
    else:
        print("  VERDICT: INCONCLUSIVE — control not written / incremental didn't engage.")
        print("           (check tmp/stp_incr.log)")
        verdict = 2
    print("--------------------------------------------------------------")
    return verdict


if __name__ == "__main__":
    sys.exit(main())
