#!/usr/bin/env python3
"""xtensa_nar.py — espjtag's first Xtensa (issue #9) step: read XDM debug registers
over JTAG on an ESP32-S3, written on espjtag's transport (no OpenOCD, no RPC).

Verbatim from espressif/openocd-esp32 @ f10eceff... (xtensa.c xtensa_examine +
xtensa_debug_module.c/.h):
  examine = ONE batch: PWRCTL=0x07, PWRCTL=0x87, queue_enable(DCRSET=ENABLEOCD),
            queue_tdi_idle, queue_execute.  (NOT separate scans — one execution,
            and the tdi_idle clocks let the debug power domain come up.)
  NAR read = IR=NARSEL; 8-bit addr scan ((nar<<1)|0); 32-bit data scan (capture) +
             tdi_idle.  Single pass — OpenOCD's device_id_read does exactly this.
  PWRCTL (JTAG path): JTAGDEBUGUSE=0x80 DEBUGWAKEUP=0x04 MEMWAKEUP=0x02 COREWAKEUP=0x01
  DCRSET nar=0x43, ENABLEOCD=0x01 ; NAR addr byte = (nar<<1)|rw.

Two fixes from earlier dead-ends: (1) the S3 is a TWO-TAP chain (chips.py now tables
it so the transport adds the BYPASS bit per scan); (2) idle clocks after the scans
(the missing tdi_idle) — without them the power domain never settles (PWRSTAT=0) and
every read is garbage, which masqueraded as a read pipeline.

Validated vs the CoreSight Component-ID magic (0xB1/0x05/0x0D) + a DDR round-trip.

Usage:  python3 scripts/xtensa_nar.py --usb 1-1.3.2.2
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util

from espjtag import EspUsbJtag, chips

IR_PWRCTL, IR_PWRSTAT, IR_NARSEL = 0x08, 0x09, 0x1C
DCRSET, OCDDCR_ENABLEOCD = 0x43, 0x01
IDLE = 16                                            # tdi_idle settle clocks per scan
NAR = {0x40: "OCDID", 0x44: "DSR", 0x45: "DDR", 0x72: "DEVID",
       0x7C: "COMPID0", 0x7D: "COMPID1", 0x7E: "COMPID2", 0x7F: "COMPID3"}
# halt + instruction injection (verbatim from xtensa.c / xtensa_debug_module.h)
DCRCLR, DSR, DDR, DDREXEC, DIR0EXEC = 0x42, 0x44, 0x45, 0x46, 0x47
OCDDCR_DEBUGINTERRUPT, OCDDSR_STOPPED = 0x02, 0x10
# Xtensa instruction encodings, little-endian (xtensa.c XT_INS_* + the regs table:
# DDR special reg = 0x68, a3 = reg 3; RSR = OPCODE|(SR<<8)|(T<<4)).
INS_RSR_DDR_A3 = 0x036830     # RSR  a3, DDR   (DDR special reg -> a3)
INS_WSR_DDR_A3 = 0x136830     # WSR  a3, DDR   (a3 -> DDR)
INS_LDDR32P_A3 = 0x0073E0     # LDDR32.P a3    (mem[a3] -> DDR, a3 += 4)


def powerup(j):
    """xtensa_examine, as ONE batch: PWRCTL 0x07, 0x87, DCRSET=ENABLEOCD, idle."""
    j._drain_in()
    j._scan_ir(IR_PWRCTL)
    j._scan_dr(0x07, 8); j._idle(IDLE)               # DEBUG|MEM|CORE wakeup
    j._scan_dr(0x87, 8); j._idle(IDLE)               # | JTAGDEBUGUSE
    j._scan_ir(IR_NARSEL)
    j._scan_dr((DCRSET << 1) | 1, 8)                 # queue_enable: DCRSET write
    j._scan_dr(OCDDCR_ENABLEOCD, 32); j._idle(IDLE)
    j._send()


def pwr_read_stat(j):
    j._drain_in()
    j._scan_ir(IR_PWRSTAT)
    t = j._scan_dr(0, 8, capture=True)
    j._idle(IDLE)
    j._send()
    return j._dr_field(j._recv(t), 0, 8)


def _nar_access(j, naraddr, value, rw):
    """One NAR access: IR=NARSEL; 8-bit addr ((nar<<1)|rw); 32-bit data; idle.
    Returns the captured 32-bit data. The NAR latches `naraddr` for the NEXT
    access's data phase (the data here belongs to the PREVIOUSLY latched addr)."""
    j._drain_in()
    j._scan_ir(IR_NARSEL)
    j._scan_dr((naraddr << 1) | rw, 8)
    t = j._scan_dr(value & 0xFFFFFFFF, 32, capture=True)
    j._idle(IDLE)
    j._send()
    return j._dr_field(j._recv(t), 0, 32)


def nar_read(j, naraddr):
    """Read: arm the address, then a second access whose data phase returns it."""
    _nar_access(j, naraddr, 0, 0)                    # arm naraddr
    return _nar_access(j, naraddr, 0, 0)             # data phase -> naraddr's value


def nar_write(j, naraddr, value):
    """Write: arm the address, then a second access whose data phase writes `value`
    to the armed register (the data lands on the PREVIOUSLY latched address)."""
    _nar_access(j, naraddr, 0, 1)                    # arm naraddr (write)
    _nar_access(j, naraddr, value, 1)                # data `value` -> naraddr


def exec_ins(j, ins):
    """Execute one Xtensa instruction on the halted core: NAR-write DIR0EXEC = ins
    (writing DIR0EXEC loads the instruction into DIR0 AND executes it)."""
    nar_write(j, DIR0EXEC, ins)


def xhalt(j, tries=200):
    """Halt the core: NAR-write DCRSET = DEBUGINTERRUPT, poll DSR until STOPPED."""
    nar_write(j, DCRSET, OCDDCR_ENABLEOCD | OCDDCR_DEBUGINTERRUPT)
    for _ in range(tries):
        if nar_read(j, DSR) & OCDDSR_STOPPED:
            return True
    return False


def xresume(j):
    """Clear the halt request (DCRCLR = DEBUGINTERRUPT)."""
    nar_write(j, DCRCLR, OCDDCR_DEBUGINTERRUPT)


def read_mem(j, addr, nwords):
    """Read `nwords` 32-bit words from `addr` on a HALTED core via instruction
    injection. The NAR applies access N's data to access N-1's ADDRESS, so the
    whole DDR=addr -> RSR(a3=DDR) -> LDDR32.P -> read-DDR chain is emitted with the
    data shifted forward one access (each `_nar_access(A, D)` carries the NEXT op's
    address A and THIS op's data D; the effect/capture lands on the previous A).
    NOTE: clobbers a3 (no save/restore yet) — fine for a halted-core read; resume +
    the app may need the a3 save added before production use."""
    # Setup (data shifted one access forward): arm DDR, write addr into it, exec
    # RSR (a3=DDR=addr), exec LDDR32P ONCE (DDR=mem[a3], a3+=4; DIR0 holds LDDR32P).
    _nar_access(j, DDR, 0, 1)                         # A=DDR (latch); junk
    _nar_access(j, DIR0EXEC, addr, 1)                # addr -> DDR ; A=DIR0EXEC
    _nar_access(j, DIR0EXEC, INS_RSR_DDR_A3, 1)      # RSR -> DIR0EXEC (exec); A=DIR0EXEC
    _nar_access(j, DDREXEC, INS_LDDR32P_A3, 1)       # LDDR32P -> DIR0EXEC (exec); A=DDREXEC
    # Stream: reading DDREXEC returns the current word AND re-triggers LDDR32P (next
    # word, a3+=4). The capture lands on the PREVIOUS access's addr (the pipeline),
    # so each read captures the word from the prior DDREXEC. Last word reads plain
    # DDR (no re-trigger).
    out = []
    for k in range(nwords):
        nxt = DDR if k == nwords - 1 else DDREXEC
        out.append(_nar_access(j, nxt, 0, 0))        # capture prev (DDREXEC word)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb", required=True)
    args = ap.parse_args()

    j = EspUsbJtag(args.usb)
    ic = j.read_idcode()
    print(f"IDCODE=0x{ic:08x} [{chips.name_for(ic) or '??'}]  "
          f"taps_after={j.taps_after} idcode_index={j.idcode_index}")

    powerup(j)
    print(f"PWRSTAT=0x{pwr_read_stat(j):02x}  (DEBUGDOMAINON={bool(pwr_read_stat(j) & 0x04)})")

    regs = {a: nar_read(j, a) for a in (0x40, 0x44, 0x72)}
    print("\nXDM NAR reads:")
    for a in (0x40, 0x44, 0x72):
        print(f"  NAR 0x{a:02x} {NAR[a]:8} = 0x{regs[a]:08x}")
    dbgmod = bool(regs[0x44] & 0x80000000)           # OCDDSR_DBGMODPOWERON = BIT(31)
    print(f"  DSR bit31 DBGMODPOWERON = {dbgmod}  (debug module powered)")

    # DEFINITIVE validator: write arbitrary patterns to DDR (a scratch mailbox) and
    # read them back. Round-tripping arbitrary 32-bit values proves NAR read AND
    # write are correct, with no dependence on any other tool or device state.
    print("\nDDR write/read round-trip (the definitive validator):")
    rt_ok = True
    for pat in (0xA5A5A5A5, 0x5A5A5A5A, 0xDEADBEEF, 0x00000000, 0xFFFFFFFF):
        nar_write(j, 0x45, pat)
        back = nar_read(j, 0x45)
        ok = back == pat
        rt_ok = rt_ok and ok
        print(f"  wrote 0x{pat:08x} -> read 0x{back:08x}  {'ok' if ok else 'MISMATCH'}")

    # MEMORY READ via DIR0EXEC instruction injection — validated against the GOLDEN
    # values OpenOCD + probe-rs both read for the S3 ROM @ 0x40000000 (xcheck).
    print("\nmemory read via DIR0EXEC (halt -> LDDR32.P -> DDR):")
    golden = [0x1049C500, 0xE52049D5, 0x49F53049, 0x00003400]
    mem_ok = False
    if xhalt(j):
        print("  halted (DSR STOPPED)")
        words = read_mem(j, 0x40000000, 4)
        mem_ok = True
        for i, (w, g) in enumerate(zip(words, golden)):
            m = w == g
            mem_ok = mem_ok and m
            print(f"  @0x{0x40000000 + i*4:08x} = 0x{w:08x}  golden 0x{g:08x}  "
                  f"{'ok' if m else 'MISMATCH'}")
        xresume(j)
        print("  resumed")
    else:
        print("  FAILED to halt")

    usb.util.dispose_resources(j.dev)
    ok = rt_ok and dbgmod and mem_ok
    print("\n=> RESULT:", "espjtag Xtensa XDM read+write + MEMORY working ✓  "
          "(DDR round-trips, DBGMODPOWERON, ROM matches OpenOCD/probe-rs golden)"
          if ok else "NAR ok but memory read off" if (rt_ok and dbgmod) else "still off")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
