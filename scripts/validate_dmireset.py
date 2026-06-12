#!/usr/bin/env python3
"""validate_dmireset.py — bench evidence for #25 (DTMCS DMIRESET on DMI busy retry).

The previous DMIRESET attempt regressed by desyncing the IN-endpoint byte
accounting (non-capture DTMCS scan + send). This proves the landed fix
(transport._dtmcs_dmireset, commit 733dd7f) keeps the accounting exact:

  [A] selftest x3 with drain_mode='validate' (every check raises on stale bytes)
  [B] DMIRESET hammer: interleave explicit _dtmcs_dmireset() with DMI reads,
      _validate_every pinned to 1 so EVERY _drain_in asserts the endpoint is
      empty. The old bug raises RuntimeError('drain-validate ... stale bytes')
      here; values must also stay deterministic.

Run per chip (C6 single-TAP, C5 two-TAP):
  python3 scripts/validate_dmireset.py --usb-path 1-1.3.3.3   # c6-maker-a
  python3 scripts/validate_dmireset.py --usb-path 1-1.2       # c5-xiao-a
"""
import argparse
import sys

sys.path.insert(0, ".")
import usb.util
from espjtag import EspUsbJtag, selftest
from espjtag.transport import EspUsbJtagTransport
from espjtag.constants import DMSTATUS


def hammer(usb_path, iters=16):
    j = EspUsbJtag(usb_path)
    try:
        j._validate_every = 1
        j._validate_ok = -(10 ** 9)          # block the check-interval backoff
        ic0 = j.read_idcode()
        ds0, st0 = j.dmi_read(DMSTATUS)
        assert st0 == 0, f"baseline dmstatus op-status {st0}"
        for i in range(iters):
            j._dtmcs_dmireset()              # the #25 path, explicitly
            ds, st = j.dmi_read(DMSTATUS)
            assert st == 0 and ds == ds0, \
                f"iter {i}: dmstatus 0x{ds:08x}(st{st}) != baseline 0x{ds0:08x}(st{st0})"
        ic1 = j.read_idcode()
        assert ic1 == ic0, f"IDCODE drifted 0x{ic0:08x} -> 0x{ic1:08x}"
        print(f"  hammer: {iters}x DMIRESET+DMI-read, validate_every=1, "
              f"IDCODE=0x{ic0:08x} dmstatus=0x{ds0:08x} stable, 0 stale bytes  PASS")
        return True
    finally:
        usb.util.dispose_resources(j.dev)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb-path", required=True)
    args = ap.parse_args()

    EspUsbJtagTransport.drain_mode = "validate"
    print(f"[A] selftest x3, drain_mode=validate, usb_path={args.usb_path}")
    passed, total = selftest(args.usb_path, rounds=3)

    print(f"[B] DMIRESET hammer, usb_path={args.usb_path}")
    ok = hammer(args.usb_path)

    good = passed == total and ok
    print(f"VERDICT: {'PASS' if good else 'FAIL'} (selftest {passed}/{total}, hammer {ok})")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
