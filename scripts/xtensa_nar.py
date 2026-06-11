#!/usr/bin/env python3
"""xtensa_nar.py — espjtag reads ESP32-S3 memory over JTAG via the Xtensa XDM,
written entirely on espjtag's transport (no OpenOCD, no RPC). Issue #9.

Model — verbatim from OpenOCD (xtensa.c / xtensa_debug_module.c) and confirmed
register-for-register against OpenOCD's own `drscan` on the live S3:
  * The S3 is a TWO-TAP chain (chips.py tables it; the transport adds the BYPASS bit).
  * Select IR=NARSEL ONCE per operation, then SINGLE 8-bit addr scan ((nar<<1)|rw)
    + 32-bit data scan per access. No re-selecting IR per access, no double-read —
    re-selecting IR was what introduced the phantom "pipeline" earlier.
  * Power-up (one batch + idle): PWRCTL=0x07, 0x87, DCRSET=ENABLEOCD.
  * Halt: DCRSET=DEBUGINTERRUPT, poll DSR STOPPED.
  * Memory read (halted): DDR=addr; exec RSR a3,DDR (a3=addr); exec LDDR32.P a3 once
    (DDR=mem[a3], a3+=4); then stream — read DDREXEC (returns the word AND re-triggers
    the load) for all but the last word, read plain DDR for the last.

Validated: DDR round-trips arbitrary patterns AND a 4-word read of 0x40000000 matches
the golden values OpenOCD + probe-rs both read (xcheck).

Usage:  python3 scripts/xtensa_nar.py --usb 1-1.3.2.2   (or bash scripts/xnar.sh)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util

from espjtag import EspUsbJtag, chips

IR_PWRCTL, IR_PWRSTAT, IR_NARSEL = 0x08, 0x09, 0x1C
# NAR register addresses (xtensa_debug_module.h xdm_regs[].nar)
DCRSET, DCRCLR, DSR, DDR, DDREXEC, DIR0EXEC = 0x43, 0x42, 0x44, 0x45, 0x46, 0x47
OCDDCR_ENABLEOCD, OCDDCR_DEBUGINTERRUPT, OCDDSR_STOPPED = 0x01, 0x02, 0x10
IDLE = 16
# Xtensa instruction encodings, little-endian (DDR special reg 0x68, a3 = reg 3)
INS_RSR_DDR_A3 = 0x036830     # RSR  a3, DDR
INS_LDDR32P_A3 = 0x0073E0     # LDDR32.P a3  (mem[a3] -> DDR, a3 += 4)


def nar_read(j, naraddr):
    """IR=NARSEL once; read the register 3x (PRIME 2 + 1) to flush espjtag's 2-access
    NAR read latency, return the last. Only valid for side-effect-free registers
    (OCDID/DSR/DDR — NOT DDREXEC, which re-triggers on every read)."""
    j._drain_in()
    j._scan_ir(IR_NARSEL)
    val = 0
    for _ in range(3):
        j._scan_dr((naraddr << 1) | 0, 8)
        t = j._scan_dr(0, 32, capture=True)
        j._send()
        val = j._dr_field(j._recv(t), 0, 32)
    return val


def nar_write(j, naraddr, value):
    j._drain_in()
    j._scan_ir(IR_NARSEL)
    j._scan_dr((naraddr << 1) | 1, 8)
    j._scan_dr(value & 0xFFFFFFFF, 32)
    j._idle(IDLE)
    j._send()


def powerup(j):
    j._drain_in()
    j._scan_ir(IR_PWRCTL)
    j._scan_dr(0x07, 8); j._idle(IDLE)               # DEBUG|MEM|CORE wakeup
    j._scan_dr(0x87, 8); j._idle(IDLE)               # | JTAGDEBUGUSE
    j._scan_ir(IR_NARSEL)
    j._scan_dr((DCRSET << 1) | 1, 8)                 # enable OCD
    j._scan_dr(OCDDCR_ENABLEOCD, 32); j._idle(IDLE)
    j._send()


def xhalt(j, tries=200):
    nar_write(j, DCRSET, OCDDCR_ENABLEOCD | OCDDCR_DEBUGINTERRUPT)
    for _ in range(tries):
        if nar_read(j, DSR) & OCDDSR_STOPPED:
            return True
    return False


def xresume(j):
    nar_write(j, DCRCLR, OCDDCR_DEBUGINTERRUPT)


def read_mem(j, addr, nwords):
    """Read nwords from `addr` on a HALTED core. One IR=NARSEL, single addr+data per
    access (the OpenOCD model). NOTE: clobbers a3 (no save/restore yet)."""
    j._drain_in()
    j._scan_ir(IR_NARSEL); j._send()                 # IR=NARSEL ONCE, like drscan

    def acc(naraddr, rw, data=0, cap=False):
        """One NAR access flushed on its own (mirrors a pair of drscan commands)."""
        j._scan_dr((naraddr << 1) | rw, 8); j._send()
        t = j._scan_dr(data & 0xFFFFFFFF, 32, capture=cap); j._send()
        return j._dr_field(j._recv(t), 0, 32) if cap else None

    acc(DDR, 1, addr)                                # DDR = addr
    acc(DIR0EXEC, 1, INS_RSR_DDR_A3)                 # a3 = DDR = addr
    acc(DIR0EXEC, 1, INS_LDDR32P_A3)                 # DDR = mem[a3]; a3 += 4 (word0)
    # espjtag's NAR data read lags by PRIME accesses vs OpenOCD's driver, so issue
    # PRIME extra DDREXEC reads up front to flush the latency, then the real words.
    PRIME = 2
    total = nwords + PRIME
    out = [acc(DDR if k == total - 1 else DDREXEC, 0, 0, cap=True)
           for k in range(total)]
    return out[PRIME:PRIME + nwords]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb", required=True)
    args = ap.parse_args()

    j = EspUsbJtag(args.usb)
    ic = j.read_idcode()
    print(f"IDCODE=0x{ic:08x} [{chips.name_for(ic) or '??'}]  taps_after={j.taps_after}")
    powerup(j)
    print(f"OCDID=0x{nar_read(j, 0x40):08x}  DSR=0x{nar_read(j, DSR):08x}")

    print("\nDDR write/read round-trip:")
    rt_ok = True
    for pat in (0xA5A5A5A5, 0x5A5A5A5A, 0xDEADBEEF, 0x00000000, 0xFFFFFFFF):
        nar_write(j, DDR, pat)
        back = nar_read(j, DDR)
        rt_ok = rt_ok and back == pat
        print(f"  wrote 0x{pat:08x} -> read 0x{back:08x}  {'ok' if back == pat else 'X'}")

    print("\nmemory read via DIR0EXEC (halt -> LDDR32.P stream):")
    golden = [0x1049C500, 0xE52049D5, 0x49F53049, 0x00003400]
    mem_ok = False
    if xhalt(j):
        words = read_mem(j, 0x40000000, 4)
        mem_ok = words == golden
        for i, (w, g) in enumerate(zip(words, golden)):
            print(f"  @0x{0x40000000 + i*4:08x} = 0x{w:08x}  golden 0x{g:08x}  "
                  f"{'ok' if w == g else 'X'}")
        xresume(j)
    else:
        print("  FAILED to halt")

    usb.util.dispose_resources(j.dev)
    ok = rt_ok and mem_ok
    print("\n=> RESULT:", "espjtag Xtensa XDM read/write + MEMORY working ✓ "
          "(matches OpenOCD/probe-rs golden)" if ok else "still off")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
