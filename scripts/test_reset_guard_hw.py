#!/usr/bin/env python3
"""test_reset_guard_hw.py — ON-TARGET proof that the reset paths FAIL LOUDLY on a
real Xtensa part (espjtag#51). The no-hardware side is covered by
test_riscv_mock.test_reset_guard_rejects_non_riscv (a mocked S3 IDCODE); this
step confirms the same guard fires against ACTUAL S3 silicon — reads the live
IDCODE, then asserts every reset entry point raises UnsupportedCoreError WITHOUT
touching the RISC-V Debug Module (which the S3 doesn't have).

Pin the S3 by --serial (the USB MAC, stable across re-enumeration) or --usb
(bus-port path). Defaults to the bench S3 fixture. Exit 0 iff the guard fires on
every path; exit 1 (with detail) if any reset path proceeds instead of refusing.

Run by check.py --real. NON-DESTRUCTIVE: the whole point is that nothing is
written to the target — if the guard works, no reset ever happens.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util                                                # noqa: E402

from espjtag import chips                                      # noqa: E402
from espjtag.debug import EspUsbJtag                           # noqa: E402
from espjtag.reset import reset_run as reset_run_transport     # noqa: E402

S3_SERIAL = os.environ.get("S3_MAC", "1C:DB:D4:76:82:08")      # xiao-s3-sense
_fail = 0


def check(label, ok, detail=""):
    global _fail
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"\n      {detail}"))
    _fail |= (not ok)


def _expect_guard(label, fn):
    """Call fn(); pass iff it raised UnsupportedCoreError (the guard fired)."""
    try:
        fn()
    except chips.UnsupportedCoreError as e:
        check(label, True)
        return str(e)
    except Exception as e:                                     # noqa: BLE001
        check(label, False, f"raised {type(e).__name__} instead of the guard: {e}")
        return None
    check(label, False, "NO exception — a reset path proceeded on an Xtensa target!")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb", default=None, help="S3 bus-port path (volatile)")
    ap.add_argument("--serial", default=None, help="S3 USB MAC (preferred; stable)")
    args = ap.parse_args()
    serial = args.serial or (None if args.usb else S3_SERIAL)

    print("=== reset guard on REAL Xtensa silicon (espjtag#51) ===")

    # Confirm we're actually pointed at an S3 (Xtensa) before proving the guard.
    j = EspUsbJtag(args.usb, serial=serial) if serial else EspUsbJtag(args.usb)
    ic = j.read_idcode()
    name = chips.name_for(ic) or "?"
    check(f"target is a known Xtensa part (got {name} 0x{ic:08x})",
          not chips.is_riscv(ic) and chips.lookup(ic) is not None,
          f"expected an Xtensa S2/S3; is_riscv={chips.is_riscv(ic)}")

    # debug.py EspUsbJtag.reset_run — the halt/DMI reset method.
    msg = _expect_guard("EspUsbJtag.reset_run() refuses the S3", lambda: j.reset_run(log=None))
    if msg:
        print(f"      -> {msg}")
    # debug.py reset_run_from_rom — must refuse BEFORE its USB bus reset.
    _expect_guard("EspUsbJtag.reset_run_from_rom() refuses the S3",
                  lambda: j.reset_run_from_rom(log=None))
    usb.util.dispose_resources(j.dev)

    # reset.py transport-only reset_run — the minimal esptool-liftable path,
    # opens its own handle (pin by the same serial).
    _expect_guard("reset.reset_run() (transport path) refuses the S3",
                  lambda: reset_run_transport(usb_path=args.usb, serial=serial))

    print("  (target NOT reset — the guard blocked every path)" if not _fail
          else "  FAIL — a reset path did NOT refuse; see above")
    return _fail


if __name__ == "__main__":
    sys.exit(main())
