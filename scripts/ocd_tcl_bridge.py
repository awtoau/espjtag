#!/usr/bin/env python3
"""ocd_tcl_bridge.py — run OpenOCD's verbatim config Tcl, backed entirely by espjtag.

Design (locked in): mini_jimtcl (pure Python, zero dependencies) as the sole Tcl
engine + OpenOCD's leaf hardware commands (mww/mdw/riscv dmi_read|write/poll)
implemented HERE on espjtag's own transport. No OpenOCD process, no RPC — anything
espjtag lacks, we write ourselves.

CHIP-AWARE: the SAME Tcl runs on either core. mww/mdw use the RISC-V System Bus on a
C6, or the Xtensa XDM (espjtag.xtensa — instruction injection) on an S3. So:
  * C6: OpenOCD's esp32c6_wdt_disable / esp32c6_soc_reset procs (verbatim from
    tcl/target/esp32c6.cfg) run UNMODIFIED — no transcription into chips.py, no drift.
  * S3: `mdw`/`mww` reach memory over the Xtensa XDM, so an S3 OpenOCD config runs the
    same way (demonstrated below: `mdw 0x40000000 4` -> the OpenOCD/probe-rs golden).

SPEED — and this matters: espjtag is ALREADY faster than OpenOCD/probe-rs. Its
_dmi_batch issues a whole burst behind ONE IR=DMI select, FIFO-chunked at the
device's IN limit, instead of OpenOCD's per-access round-trips. So this bridge runs
OpenOCD's verbatim Tcl AT espjtag's (faster) speed: bulk `mdw` routes through the
batched read_mem; scattered `mww` (wdt_disable's regs aren't contiguous) is one
write each in OpenOCD too — no regression. PARALLEL, not a replacement: espjtag's
native chips.py/debug.py paths stay primary; this is the SAME logic via the upstream
Tcl, kept side-by-side until proven — a correctness hedge, NOT because it's slower.

Validate against OpenOCD -d3 (the golden trace), not flaky device values.

Usage:  python3 scripts/ocd_tcl_bridge.py --usb 1-1.3.3.3 [--reset]
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import usb.util

from espjtag import EspUsbJtag, chips
from espjtag.constants import DMCONTROL, DMSTATUS, SBCS, SBADDRESS0, SBDATA0
from espjtag.xtensa import XtensaXDM
from mini_jimtcl import MiniJimTcl

# verbatim from openocd-esp32 tcl/target/esp32c6.cfg
ESP32C6_WDT_DISABLE = r"""
proc esp32c6_wdt_disable { } {
    if { [string compare [command mode] config] == 0 } { return }
    mww 0x60008064 0x50D83AA1
    mww 0x60008048 0
    mww 0x6000807C 0x2
    mww 0x60009064 0x50D83AA1
    mww 0x60009048 0
    mww 0x6000907C 0x2
    mww 0x600b1c18 0x50D83AA1
    mww 0x600B1c00 0
    mww 0x600b1c20 0x50D83AA1
    mww 0x600b1c1c 0x40000000
    mww 0x600B1c30 0xC0000000
}
"""

ESP32C6_SOC_RESET = r"""
proc esp32c6_soc_reset { } {
    global _RISCV_DMCONTROL _RISCV_SB_CS _RISCV_SB_ADDR0 _RISCV_SB_DATA0
    riscv dmi_write $_RISCV_DMCONTROL 0x80000001
    riscv dmi_write $_RISCV_SB_CS 0x48000
    riscv dmi_write $_RISCV_SB_ADDR0 0x600b1034
    riscv dmi_write $_RISCV_SB_DATA0 0x80000000
    riscv dmi_write $_RISCV_DMCONTROL 0
    riscv dmi_write $_RISCV_SB_CS 0x48000
    riscv dmi_write $_RISCV_SB_ADDR0 0x600b1038
    riscv dmi_write $_RISCV_SB_DATA0 0x10000000
    riscv dmi_write $_RISCV_DMCONTROL 0
    riscv dmi_write $_RISCV_DMCONTROL 0x40000001
    sleep 10
    poll
    esp32c6_wdt_disable
    riscv dmi_write $_RISCV_DMCONTROL 0x40000001
    riscv dmi_write $_RISCV_DMCONTROL 0x80000003
}
"""


class OcdTclBridge:
    """A MiniJimTcl with OpenOCD's leaf hardware commands wired to espjtag."""

    def __init__(self, j):
        self.j = j
        self.trace = []                     # (op, addr, val) — for -d3 comparison
        # On a RISC-V part mww/mdw use System Bus Access; on Xtensa (S3) they route
        # to the XDM (instruction injection) — same Tcl, the right hardware path.
        self.core = (chips.lookup(j.read_idcode()) or {}).get("core", "riscv")
        self.xdm = XtensaXDM(j) if self.core == "xtensa" else None
        self.tcl = MiniJimTcl()
        t = self.tcl
        t.register("mww", self._mww)
        t.register("mdw", self._mdw)
        t.register("riscv", self._riscv)
        t.register("command", lambda a: "exec")
        t.register("poll", self._poll)
        t.register("sleep", self._sleep)
        t.register("echo", lambda a: (print(*a), "")[1])
        if self.core == "riscv":
            # the DMI-register globals esp32c6_soc_reset reads (just the DMI
            # addresses espjtag already knows), and the verbatim C6 procs.
            t.vars.update({
                "_RISCV_DMCONTROL": str(DMCONTROL), "_RISCV_SB_CS": str(SBCS),
                "_RISCV_SB_ADDR0": str(SBADDRESS0), "_RISCV_SB_DATA0": str(SBDATA0),
            })
            t.eval(ESP32C6_WDT_DISABLE)
            t.eval(ESP32C6_SOC_RESET)

    # --- OpenOCD leaf commands, on espjtag's transport (RISC-V SBA or Xtensa XDM) -
    def _mww(self, args):
        addr, val = int(args[0], 0), int(args[1], 0)
        self.trace.append(("mww", addr, val))
        (self.xdm.write_mem32 if self.xdm else self.j.write_mem32)(addr, val)
        return ""

    def _mdw(self, args):
        addr = int(args[0], 0)
        n = int(args[1]) if len(args) > 1 else 1
        if self.xdm:
            vals = self.xdm.read_mem(addr, n)
        else:
            vals = self.j.read_mem(addr, n) if n > 1 else [self.j.read_mem32(addr)]
        return " ".join(f"0x{v:08x}" for v in vals)

    def _riscv(self, args):
        if args[0] == "dmi_read":
            addr = int(args[1], 0)
            data, _ = self.j.dmi_read(addr)
            self.trace.append(("dmi_read", addr, data))
            return str(data)
        if args[0] == "dmi_write":
            addr, val = int(args[1], 0), int(args[2], 0)
            self.trace.append(("dmi_write", addr, val))
            self.j.dmi_write(addr, val)
            return ""
        raise RuntimeError(f"riscv: unsupported subcommand {args[0]!r}")

    def _poll(self, args):
        return f"0x{self.j.dm_read(DMSTATUS):08x}"

    def _sleep(self, args):
        # Repo rule: NO wall-clock sleep. OpenOCD's `sleep 10; poll` waits for the
        # SoC reset to propagate; espjtag relies on USB latency + the following
        # poll instead (its native reset path does the same). Intentional no-op.
        return ""

    def run(self, tcl):
        return self.tcl.eval(tcl)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb", required=True)
    ap.add_argument("--reset", action="store_true",
                    help="also run esp32c6_soc_reset (RESETS the chip)")
    args = ap.parse_args()

    j = EspUsbJtag(args.usb)
    ic = j.read_idcode()
    br = OcdTclBridge(j)
    print(f"IDCODE=0x{ic:08x} [{chips.name_for(ic) or '??'}]  core={br.core}")

    if br.core == "xtensa":
        # S3: leaf commands run on the Xtensa XDM. Demonstrate a Tcl `mdw` end-to-end.
        br.xdm.powerup()
        if not br.xdm.halt():
            print("FAILED to halt"); return 1
        print("halted via XDM. Running 'mdw 0x40000000 4' through mini_jimtcl:")
        out = br.run("mdw 0x40000000 4")
        golden = "0x1049c500 0xe52049d5 0x49f53049 0x00003400"
        print(f"  mdw -> {out}")
        print(f"  golden {golden}  {'MATCH' if out == golden else 'MISMATCH'}")
        br.xdm.resume()
        usb.util.dispose_resources(j.dev)
        return 0 if out == golden else 1

    # RISC-V (C6): run OpenOCD's verbatim wdt_disable, read back, optional soc_reset.
    j.examine()
    if not j.halt(disable_wdt=False):       # halt WITHOUT espjtag's native WDT disable
        print("FAILED to halt"); return 1
    print("halted (native WDT-disable skipped — the bridge will do it)")
    print("\nrunning OpenOCD's VERBATIM esp32c6_wdt_disable via espjtag...")
    br.run("esp32c6_wdt_disable")
    for op, a, v in br.trace:
        print(f"  {op} 0x{a:08x} 0x{v:08x}")
    print("\nread-back (proves the writes landed on real silicon):")
    for name, reg, want in (("TG0 cfg", 0x60008048, 0), ("TG1 cfg", 0x60009048, 0),
                            ("LP_WDT_SWD cfg", 0x600B1C1C, 0x40000000)):
        got = j.read_mem32(reg)
        print(f"  {name:14} 0x{reg:08x} = 0x{got:08x}  {'ok' if got == want else 'MISMATCH'}")
    if args.reset:
        print("\nrunning esp32c6_soc_reset (verbatim)...")
        br.run("esp32c6_soc_reset")
        print(f"  dmstatus now 0x{j.dm_read(DMSTATUS):08x}")
    else:
        j.resume()
        print("\nresumed (pass --reset to also run the verbatim soc_reset)")
    usb.util.dispose_resources(j.dev)
    return 0


if __name__ == "__main__":
    sys.exit(main())
