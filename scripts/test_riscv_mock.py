#!/usr/bin/env python3
"""test_riscv_mock.py — NO-HARDWARE test of the RISC-V debug sequences against
MockEspUsbJtag. Validates that espjtag emits the RIGHT Debug-Module-Interface
(DMI) writes — e.g. reset_run's golden OpenOCD `reset run` sequence — with ZERO
JTAG. Run by check.py --mock. Exit 1 on any mismatch.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from espjtag.mock import MockEspUsbJtag                        # noqa: E402
from espjtag.debug import EspUsbJtag                           # noqa: E402
from espjtag.constants import (                                # noqa: E402
    DMCONTROL, DM_DMACTIVE, DM_RESUMEREQ, DM_HALTREQ, DM_NDMRESET)

_fail = 0


def check(label, got, want):
    global _fail
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
          + ("" if ok else f"\n      got  {got}\n      want {want}"))
    _fail |= (not ok)


def test_reset_run_dmi_sequence():
    """reset_run must emit the OpenOCD `reset run` DMI sequence (debug.py:1026,
    captured from OpenOCD -d3): claim DM, resumereq, haltreq|ndmreset, then
    deassert ndmreset + resume. Golden first 3 DMCONTROL writes."""
    j = MockEspUsbJtag()
    EspUsbJtag.reset_run(j)
    dmcontrol = [v for a, v in j.dmi_writes if a == DMCONTROL]
    golden3 = [
        DM_DMACTIVE,                                    # 0x00000001 claim DM
        DM_RESUMEREQ | DM_DMACTIVE,                     # 0x40000001
        DM_HALTREQ | DM_NDMRESET | DM_DMACTIVE,         # 0x80000003 assert reset
    ]
    check("reset_run emits >= 3 DMCONTROL writes", len(dmcontrol) >= 3, True)
    check("reset_run first 3 DMCONTROL == golden OpenOCD seq",
          dmcontrol[:3], golden3)
    # ndmreset must be DEASSERTED before the final resume (bit cleared)
    check("reset_run deasserts ndmreset (no NDMRESET in last write)",
          bool(dmcontrol[-1] & DM_NDMRESET), False)


def main():
    print("=== RISC-V debug sequences — NO-HARDWARE (software model) ===")
    test_reset_run_dmi_sequence()
    print("  (no hardware was touched)")
    return _fail


if __name__ == "__main__":
    sys.exit(main())
