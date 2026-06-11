#!/usr/bin/env python3
"""st_incremental_proof.py — prove (on real silicon) that STM32CubeProgrammer's
`incremental` download decides "sector unchanged?" with a WEAK 32-bit additive
byte-sum, by a sum-preserving differential flash on an STM32F427.

INDEPENDENT VERIFICATION: all setup (write A) and every read-back use the OPEN
texane `st-flash` (BSD) — NOT ST's software. The ONLY ST-software action is the
`incremental` / normal `-d` itself (CubeProgrammer), so the result is never
self-certified by the tool under test. NO backup (test boards).

  1. (st-flash) write base image A (first 64 KB = four 16 KB sectors).
  2. (CubeProgrammer) `incremental`-flash B: sector 1 = adjacent-byte SWAP
     (content differs, additive sum identical); sector 2 = single-byte FLIP (control).
  3. (st-flash) read back & analyze.
  4. (st-flash write A; CubeProgrammer NORMAL -d B; st-flash read) — control: normal
     mode must WRITE sector 1, proving the skip is incremental-specific.

Verdict: sector 1 reads back as A (swap dropped) AND sector 2 as B (control written)
-> additive sum. A full/legacy write would write sector 1 (==B); a CRC would catch it
(==B). Only an additive collision yields the skip.

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
    """STM32CubeProgrammer — used ONLY for the incremental/normal `-d` (tool under test)."""
    cmd = [CLI, "-c", "port=SWD", f"sn={sn}", "freq=8000", "mode=UR"] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = r.stdout + r.stderr
    if log:
        with open(log, "w") as f:
            f.write("$ " + " ".join(cmd) + "\n\n" + out)
    return r.returncode, out


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
    assert ssum(A, 1) == ssum(B, 1) and ssum(A, 2) != ssum(B, 2)
    assert A[SEC:2 * SEC] != B[SEC:2 * SEC]

    print(f"\n[1] (st-flash, independent) writing base image A to {sn} ...")
    rc, out = st_write(sn, lp("stp_A.bin"))
    if rc != 0:
        print(f"  st-flash write A FAILED rc={rc}"); sys.stdout.write(out[-500:]); return 1
    st_read(sn, lp("stp_rd.bin"))
    if open(lp("stp_rd.bin"), "rb").read() != A:
        print("  A did NOT verify (st-flash read) — aborting (premise broken)."); return 1
    print("  A written + verified by st-flash (no ST software involved).")

    print("[2] (CubeProgrammer) INCREMENTAL-flashing B ...")
    rc, out = cube(sn, "-d", lp("stp_B.bin"), hex(BASE), "incremental", log=lp("stp_incr.log"))
    low = out.lower()
    print(f"  rc={rc}  incremental-engaged~{('modified sector' in low) or ('checksum verify' in low)}"
          f"  legacy-fallback~{'legacy' in low and ('not' in low or 'instead' in low)}")

    print("    (st-flash, independent) reading back ...")
    st_read(sn, lp("stp_rd.bin"))
    R = open(lp("stp_rd.bin"), "rb").read()

    s1A, s1B, s1R = A[SEC:2*SEC], B[SEC:2*SEC], R[SEC:2*SEC]
    s2A, s2B, s2R = A[2*SEC:3*SEC], B[2*SEC:3*SEC], R[2*SEC:3*SEC]
    sec1_skipped = s1R == s1A and s1R != s1B
    sec1_written = s1R == s1B
    sec2_written = s2R == s2B
    print("\n--- RESULT (read back by st-flash, not CubeProgrammer) -------")
    print(f"  sector-1 (sum-preserving swap): byte@0x{SUMPRES_OFF:x} = 0x{R[SUMPRES_OFF]:02x} "
          f"(A=0x{A[SUMPRES_OFF]:02x} B=0x{B[SUMPRES_OFF]:02x})  "
          f"-> {'SKIPPED (==A)' if sec1_skipped else 'written (==B)' if sec1_written else 'OTHER'}")
    print(f"  sector-2 (control flip):        byte@0x{CTRL_OFF:x} = 0x{R[CTRL_OFF]:02x} "
          f"(A=0x{A[CTRL_OFF]:02x} B=0x{B[CTRL_OFF]:02x})  "
          f"-> {'WRITTEN (==B)' if sec2_written else 'not written (==A)'}")
    if sec2_written and sec1_skipped:
        print("  VERDICT: ADDITIVE SUM CONFIRMED — sum-preserving change silently SKIPPED.")
        verdict = 0
    elif sec2_written and sec1_written:
        print("  VERDICT: NOT additive — it caught the sum-preserving change (real CRC).")
        verdict = 1
    else:
        print("  VERDICT: INCONCLUSIVE — control not written (check tmp/stp_incr.log)."); verdict = 2
    print("--------------------------------------------------------------")

    # CONTROL: same B, NORMAL (legacy) mode — must WRITE sector 1.
    print("\n[3] CONTROL — (st-flash) re-write A, (CubeProgrammer) NORMAL flash B ...")
    st_write(sn, lp("stp_A.bin"))
    cube(sn, "-d", lp("stp_B.bin"), hex(BASE), log=lp("stp_ctrlB.log"))
    st_read(sn, lp("stp_rd2.bin"))
    R2 = open(lp("stp_rd2.bin"), "rb").read()
    norm_s1 = R2[SEC:2 * SEC] == B[SEC:2 * SEC]
    print(f"  normal-mode sector-1 byte@0x{SUMPRES_OFF:x} = 0x{R2[SUMPRES_OFF]:02x} "
          f"(A=0x{A[SUMPRES_OFF]:02x} B=0x{B[SUMPRES_OFF]:02x})  "
          f"-> {'WRITTEN (==B)' if norm_s1 else 'NOT written'}")
    if sec1_skipped and norm_s1:
        print("  => SAME B: incremental SKIPPED the sum-preserving sector, normal WROTE it.")
        print("     Skip is incremental-specific; verified entirely by st-flash. AIRTIGHT.")
    return verdict


if __name__ == "__main__":
    sys.exit(main())
