#!/usr/bin/env python3
"""test_xtensa_step.py — ON-TARGET single-step test for the #48 general
resume/single-step port (xtensa_fetch_all_regs + xtensa_do_step + the DEBUGCAUSE
branch in xtensa_prepare_resume).

STATUS (#48): NOT yet passing on hardware — but NOT because of a port bug. The
port is faithful (mock CI 7/7 green) and fetch_all_regs is correct: on a bare /
idle S3 the debug PC genuinely reads EPC6=0 (verified — the trusted flasher-path
`_get_sr_a3(EPC6)` reads 0 too), so there is no running-core context to step. A
true single-step validation needs a fixture that puts the core at a known,
running PC with the FULL context the flasher establishes (windowbase/windowstart/
ps/vecbase/all-aregs/stack), then steps from the trampoline's first real
instruction — accounting for the S3 reversed-memory (instruction vs data bus)
aliasing. That fixture is the open work tracked in issue #48.

This script's current form (load a stub, set PC, step) does NOT establish that
full context, so it does not yet pass; kept as the starting point for the fixture.

    python3 scripts/test_xtensa_step.py --usb 1-1.3.3.4

Output also -> tmp/test_xtensa_step.log. Needs a real S3 (s3-xiao-sense).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util                                                  # noqa: E402
from espjtag import EspUsbJtag                                   # noqa: E402
from espjtag.xtensa import XtensaXDM, XT_REG_IDX_PC              # noqa: E402
from espjtag.xtensa_flasher import XtensaFlasher                 # noqa: E402

_fail = 0


def check(label, cond):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    _fail |= (not cond)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb", default="1-1.3.3.4")               # s3-xiao-sense
    ap.add_argument("--steps", type=int, default=5)
    args = ap.parse_args()

    j = EspUsbJtag(args.usb)
    x = XtensaXDM(j)
    x.powerup()
    print("=== ON-TARGET single-step (xtensa_do_step) ===")
    check("core halted", x.halt())

    # A bare-halted S3 parks at EPC6=0 (nothing running). To step REAL code, load a
    # stub and point the debug PC (EPC6) at its entry, then step through it.
    fl = XtensaFlasher(x, "esp32s3")
    st = fl.load("cmd_test1")
    entry = st["entry"]
    x.xtensa_fetch_all_regs()                    # populate the cache
    x.xtensa_reg_set(XT_REG_IDX_PC, entry)       # PC <- stub entry
    x.xtensa_write_dirty_registers()             # flush PC (EPC6) to the core
    pc0 = x.xtensa_reg_get(XT_REG_IDX_PC)
    cause = x.xtensa_cause_get()
    print(f"  [INFO] stub entry PC=0x{pc0:08x}  DEBUGCAUSE=0x{cause:x}")
    check("PC set to stub entry", pc0 == entry)

    prev = pc0
    advanced = 0
    for n in range(args.steps):
        x.xtensa_do_step(current=True, address=0, handle_breakpoints=False)
        pc = x.xtensa_reg_get(XT_REG_IDX_PC)
        moved = (pc != prev)
        print(f"  [INFO] step {n + 1}: PC 0x{prev:08x} -> 0x{pc:08x}"
              + ("" if moved else "  (no change!)"))
        if moved:
            advanced += 1
        prev = pc
    # Most steps should move PC. (A tight self-loop could legitimately not move,
    # so require the majority to advance rather than all.)
    check(f"PC advanced on >= {args.steps - 1}/{args.steps} steps (got {advanced})",
          advanced >= args.steps - 1)

    x.resume()
    usb.util.dispose_resources(j.dev)
    print("VERDICT:", "PASS" if not _fail else "FAIL")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
