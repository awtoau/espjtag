#!/usr/bin/env python3
"""tcl_bridge_demo.py — run OpenOCD's *config* Tcl procs VERBATIM in a Python Tcl
interpreter, backed by espjtag. This is the right use of tkinter.Tcl():

  tkinter.Tcl() gives the Tcl LANGUAGE, but not OpenOCD's hardware commands (mww,
  reg, riscv ...). We supply THOSE as Python functions via tcl.createcommand(),
  pointed at espjtag. Then OpenOCD's esp32c6_wdt_disable / esp32c6_soc_reset procs
  run UNMODIFIED — no hand-transcription into chips.py, and no upstream drift.

This demo runs esp32c6_wdt_disable (verbatim from openocd-esp32 tcl/target/
esp32c6.cfg) with a logging `mww`, and checks the register writes it emits match
chips.py's wdt dict exactly — proving the data we hand-ported equals the source,
and that the Tcl-bridge could REPLACE the transcription.

NOTE on scope: this works for Tcl-level procs (mww/mdw/riscv dmi_*). It does NOT
reach OpenOCD's C-level protocols (the Xtensa XDM NAR scans are in C, not Tcl) —
for those, drive OpenOCD itself over its Tcl RPC (scripts/ocd_rpc.py).
"""
import os
import sys
import tkinter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from espjtag import chips

# verbatim from openocd-esp32 tcl/target/esp32c6.cfg
WDT_PROC = r"""
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

writes = []


def mww(addr, val):
    """The OpenOCD `mww addr val` command, backed by Python (here: log; in a real
    bridge this is espjtag.write_mem32(addr, val))."""
    writes.append((int(str(addr), 0), int(str(val), 0)))


def command(*args):
    return "exec"               # the proc's `[command mode]` guard -> not config


def chips_py_writes():
    """Reconstruct the register writes chips.py's wdt dict represents."""
    wdt = chips.lookup(0x0000DC25)["wdt"]
    out = []
    for wkey, cfg, val in wdt["disable"]:
        out.append((wkey, wdt["key"]))      # unlock key -> WKEY
        out.append((cfg, val))              # config value
    out += [(reg, val) for reg, val in wdt["int_clear"]]
    return out


def main():
    tcl = tkinter.Tcl()
    tcl.createcommand("mww", mww)
    tcl.createcommand("command", command)
    tcl.eval(WDT_PROC)                       # define OpenOCD's verbatim proc
    tcl.eval("esp32c6_wdt_disable")          # RUN it -> dispatches into our mww

    print("register writes from OpenOCD's VERBATIM Tcl proc (via tkinter.Tcl):")
    for a, v in writes:
        print(f"  mww 0x{a:08x} 0x{v:08x}")

    exp = chips_py_writes()
    match = sorted(writes) == sorted(exp)
    print(f"\nchips.py wdt dict encodes {len(exp)} writes; the Tcl proc emitted "
          f"{len(writes)}.")
    print("=> SAME register writes:", "YES — chips.py == OpenOCD's Tcl, and this "
          "bridge could replace the transcription" if match else "NO (differs!)")
    return 0 if match else 1


if __name__ == "__main__":
    raise SystemExit(main())
