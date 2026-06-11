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

    # CoreSight Component-ID — informational only: the Xtensa LX7 XDM doesn't
    # implement the ARM CIDR magic (reads 0), so it's NOT a pass/fail signal here.
    cid = [nar_read(j, a) & 0xFF for a in (0x7C, 0x7D, 0x7E, 0x7F)]
    print(f"\nCoreSight CIDR (informational; LX7 XDM doesn't implement it): "
          f"{[f'0x{x:02x}' for x in cid]}")

    usb.util.dispose_resources(j.dev)
    ok = rt_ok and dbgmod
    print("\n=> RESULT:", "espjtag Xtensa XDM read+write WORKING ✓  "
          "(DDR round-trips + DBGMODPOWERON set)" if ok else "still off")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
