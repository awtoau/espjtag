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
    DMCONTROL, DM_DMACTIVE, DM_RESUMEREQ, DM_HALTREQ, DM_NDMRESET,
    DMSTATUS, DM_ALLHALTED, DM_ALLRUNNING, COMMAND, DATA0, ABSTRACTCS,
    CMD_ACCESS_REGISTER, AC_AARSIZE32, AC_TRANSFER, AC_WRITE,
    SBCS, SBADDRESS0, SBDATA0)

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


def test_halt_dmi_sequence():
    """halt() (debug.py:52): write HALTREQ|DMACTIVE, poll DMSTATUS for ALLHALTED,
    then drop haltreq (DMCONTROL=DMACTIVE). With disable_wdt=False (untabled mock
    chip has no wdt table anyway) no SBA writes follow."""
    j = MockEspUsbJtag()
    j.set_dm(DMSTATUS, DM_ALLHALTED)                   # script: hart is halted
    ok = EspUsbJtag.halt(j, disable_wdt=False)
    check("halt() returns True when ALLHALTED", ok, True)
    dmcontrol = [v for a, v in j.dmi_writes if a == DMCONTROL]
    check("halt first DMCONTROL == HALTREQ|DMACTIVE",
          dmcontrol[0], DM_HALTREQ | DM_DMACTIVE)
    check("halt drops haltreq after (last DMCONTROL == DMACTIVE)",
          dmcontrol[-1], DM_DMACTIVE)
    check("halt polled DMSTATUS", DMSTATUS in j.dmi_reads, True)


def test_resume_dmi_sequence():
    """resume() (debug.py:82): RESUMEREQ|DMACTIVE, poll ALLRUNNING, drop req."""
    j = MockEspUsbJtag()
    j.set_dm(DMSTATUS, DM_ALLRUNNING)
    ok = EspUsbJtag.resume(j)
    check("resume() returns True when ALLRUNNING", ok, True)
    dmcontrol = [v for a, v in j.dmi_writes if a == DMCONTROL]
    check("resume first DMCONTROL == RESUMEREQ|DMACTIVE",
          dmcontrol[0], DM_RESUMEREQ | DM_DMACTIVE)
    check("resume drops req after (last DMCONTROL == DMACTIVE)",
          dmcontrol[-1], DM_DMACTIVE)


def test_read_register_abstract_sequence():
    """read_register() (debug.py:110): write COMMAND = access-register read, wait
    !busy, read DATA0. Golden COMMAND word for an x-reg."""
    j = MockEspUsbJtag()
    j.set_dm(ABSTRACTCS, 0)                            # not busy, no cmderr
    j.set_dm(DATA0, 0xCAFEF00D)                        # the register value
    val = j.read_register(0x1008)                      # GPR x8 (0x1000+8)
    want_cmd = CMD_ACCESS_REGISTER | AC_AARSIZE32 | AC_TRANSFER | 0x1008
    cmds = [v for a, v in j.dmi_writes if a == COMMAND]
    check("read_register COMMAND == access-register|size32|transfer|regno",
          cmds[-1], want_cmd)
    check("read_register returns DATA0", val, 0xCAFEF00D)
    check("read_register did NOT set AC_WRITE", bool(cmds[-1] & AC_WRITE), False)


def test_write_register_abstract_sequence():
    """write_register() (debug.py:123): DATA0 = value, then COMMAND = access-register
    WRITE. Golden order (DATA0 before COMMAND) + the WRITE bit set."""
    j = MockEspUsbJtag()
    j.set_dm(ABSTRACTCS, 0)
    j.write_register(0x1008, 0x12345678)
    # DATA0 write must precede the COMMAND write (value staged first)
    addrs = [a for a, v in j.dmi_writes]
    check("write_register writes DATA0 before COMMAND",
          addrs.index(DATA0) < addrs.index(COMMAND), True)
    data0 = [v for a, v in j.dmi_writes if a == DATA0]
    check("write_register staged the value in DATA0", data0[-1], 0x12345678)
    want_cmd = CMD_ACCESS_REGISTER | AC_AARSIZE32 | AC_TRANSFER | AC_WRITE | 0x1008
    cmds = [v for a, v in j.dmi_writes if a == COMMAND]
    check("write_register COMMAND has AC_WRITE + regno", cmds[-1], want_cmd)


def test_write_mem32_sba_sequence():
    """write_mem32() (debug.py:194): _sb_setup, SBADDRESS0=addr, SBDATA0=value, via
    System Bus Access. Golden: SBCS setup precedes the address, address precedes the
    data (the SBA latches the bus write on the data write)."""
    j = MockEspUsbJtag()
    j.write_mem32(0x600C0000, 0xDEADBEEF)
    addrs = [a for a, v in j.dmi_writes]
    check("write_mem32 writes SBCS (setup) before SBADDRESS0",
          addrs.index(SBCS) < addrs.index(SBADDRESS0), True)
    check("write_mem32 writes SBADDRESS0 before SBDATA0",
          addrs.index(SBADDRESS0) < addrs.index(SBDATA0), True)
    sbaddr = [v for a, v in j.dmi_writes if a == SBADDRESS0]
    sbdata = [v for a, v in j.dmi_writes if a == SBDATA0]
    check("write_mem32 SBADDRESS0 == addr", sbaddr[-1], 0x600C0000)
    check("write_mem32 SBDATA0 == value", sbdata[-1], 0xDEADBEEF)


def test_read_mem32_sba_sequence():
    """read_mem32() (debug.py:188): _sb_setup(readonaddr), SBADDRESS0=addr, read
    SBDATA0 (readonaddr fetched word0 on the address write)."""
    j = MockEspUsbJtag()
    j.set_dm(SBDATA0, 0x01234567)
    val = j.read_mem32(0x3FC80000)
    sbaddr = [v for a, v in j.dmi_writes if a == SBADDRESS0]
    check("read_mem32 wrote SBADDRESS0 == addr", sbaddr[-1], 0x3FC80000)
    check("read_mem32 read SBDATA0", SBDATA0 in j.dmi_reads, True)
    check("read_mem32 returns the SBDATA0 word", val, 0x01234567)


def _wdt_table_writes(entry):
    """The (addr, value) SBA writes _wdt_disable performs from a chip's wdt table
    (debug.py:68): per disable row, unlock (wkey<-key) then config (cfg<-val), then
    the int_clear writes."""
    w = entry["wdt"]
    out = []
    for wkey, cfg, val in w["disable"]:
        out.append((wkey, w["key"]))
        out.append((cfg, val))
    out += [tuple(x) for x in w.get("int_clear", ())]
    return out


def test_c5_wdt_disable_runtime_sequence():
    """The C5 (#33/#38 family) WDT-disable RUNTIME write sequence against the mock.
    verify_chips_vs_tcl.py checks the chips.py table == OpenOCD's Tcl; THIS checks
    that _wdt_disable actually EMITS those writes, in order, via SBA — the runtime
    side the table-drift test can't see."""
    from espjtag import chips
    c5 = next(v for v in chips.CHIPS.values() if v.get("name") == "C5")
    j = MockEspUsbJtag(idcode=0x00017C25)              # C5 IDCODE -> _chip() = C5
    j.set_dm(DMSTATUS, DM_ALLHALTED)
    j.halt(disable_wdt=True)                           # halt + WDT disable
    # the (addr, value) SBA writes that landed (SBADDRESS0 then SBDATA0 pairs)
    sba = []
    pend_addr = None
    for a, v in j.dmi_writes:
        if a == SBADDRESS0:
            pend_addr = v
        elif a == SBDATA0 and pend_addr is not None:
            sba.append((pend_addr, v)); pend_addr = None
    want = _wdt_table_writes(c5)
    check("C5 _wdt_disable emitted the full table write set (as a set)",
          set(sba) >= set(want), True)
    check("C5 _wdt_disable wrote the unlock key before each config (order)",
          sba[:len(want)], want)


def main():
    print("=== RISC-V debug sequences — NO-HARDWARE (software model) ===")
    test_reset_run_dmi_sequence()
    test_halt_dmi_sequence()
    test_resume_dmi_sequence()
    test_read_register_abstract_sequence()
    test_write_register_abstract_sequence()
    test_write_mem32_sba_sequence()
    test_read_mem32_sba_sequence()
    test_c5_wdt_disable_runtime_sequence()
    print("  (no hardware was touched)")
    return _fail


if __name__ == "__main__":
    sys.exit(main())
