#!/usr/bin/env python3
"""prove_call_add.py — STAGE 1 of espjtag #3: prove the "call a function on the
target" mechanism with ZERO flash/ROM side effects.

We stage two RISC-V instructions into scratch SRAM:
    add a0, a0, a1     (0x00b50533)
    ebreak             (0x00100073)
set a0/a1 to known values, point dpc at the staged code, resume, and on the
ebreak-halt read a0. If a0 == a0_in + a1_in, we have proven: halt -> stage code
+ args in GPRs -> set PC -> resume -> trap back -> read result -> restore + resume
the app. This is exactly the primitive the ROM flash-loader call uses, but proven
here without touching flash or calling any ROM routine.

Read-modify-RESTORE the scratch SRAM and all clobbered regs, then resume the app
so the board is left running. Output to tmp/.

Usage: python3 scripts/prove_call_add.py <usb_path>   (run from the repo root)
"""
import os
import sys

# import espjtag from THIS checkout (the worktree), not whatever's on PYTHONPATH.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util

from espjtag import EspUsbJtag
from espjtag import chips
from espjtag.constants import EBREAK, REG_GPR_BASE, CSR_DPC, DMSTATUS, DM_ALLRUNNING

ADD_A0_A0_A1 = 0x00B50533       # add a0, a0, a1


def main(path):
    j = EspUsbJtag(path)
    ic = j.read_idcode()
    name = chips.name_for(ic)
    sram = chips.sram_for(ic)
    print(f"target: [{name}] IDCODE=0x{ic:08x}")
    if sram is None:
        print("  no scratch-SRAM window tabled for this chip — aborting")
        return 1
    code = sram["data"]                  # stage the 2-instr routine here
    trap = sram["trap"]
    print(f"  scratch code @ 0x{code:08x}  trap @ 0x{trap:08x}")

    j.examine()
    if not j.halt():
        print("  FAILED to halt"); return 1
    print("  halted")

    # --- save what we touch so the app is left byte-identical ---
    saved_code = j.read_mem(code, 2)
    print(f"  saved scratch words: {[hex(w) for w in saved_code]}")

    # stage: add a0,a0,a1 ; ebreak
    j.write_mem(code, [ADD_A0_A0_A1, EBREAK])
    back = j.read_mem(code, 2)
    print(f"  staged code        : {[hex(w) for w in back]} "
          f"(expect {[hex(ADD_A0_A0_A1), hex(EBREAK)]})")

    a0_in, a1_in = 0x12340000, 0x00005678
    ret, halted = j.call_function(code, args=(a0_in, a1_in),
                                  stack=sram["stack"], trap=trap)
    print(f"  call add({a0_in:#x}, {a1_in:#x}) -> a0={ret if ret is None else hex(ret)} "
          f"halted={halted}")
    expect = (a0_in + a1_in) & 0xFFFFFFFF
    ok = halted and ret == expect
    print(f"  expected 0x{expect:08x}  -> {'PASS' if ok else 'FAIL'}")

    # --- restore the scratch RAM, then resume the app ---
    j.write_mem(code, saved_code)
    chk = j.read_mem(code, 2)
    print(f"  restored scratch   : {[hex(w) for w in chk]} "
          f"({'ok' if chk == saved_code else 'MISMATCH'})")
    ran = j.resume()
    st = j.dm_read(DMSTATUS)
    print(f"  resume -> {ran}  dmstatus=0x{st:08x} allrunning={(st >> 11) & 1}")
    usb.util.dispose_resources(j.dev)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
