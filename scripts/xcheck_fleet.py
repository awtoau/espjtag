#!/usr/bin/env python3
"""xcheck_fleet.py — sweep EVERY Espressif USB-JTAG unit on the bus with xcheck.

Roster from the bench config DB (esp32-zephyr dev.py fleet-status --json). For each
CONNECTED, non-off-limits unit whose chip xcheck supports (C6/C5 RISC-V, S3 Xtensa)
we run the full differential xcheck --chip <chip>. Off-limits (production) and any
unsupported/absent unit gets a read-only IDCODE identity probe only (no halt).

OpenOCD binds :3333, so the xchecks run SEQUENTIALLY. A halt/resume cycle can wedge
a unit's USB endpoint; on an espjtag USB error we `dev.py usb-reset` it and retry once.

Usage:  python3 scripts/xcheck_fleet.py
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import usb.util
from espjtag import EspUsbJtag, chips

DEV_PY = "/home/dan/git/esp32-zephyr/scripts/dev.py"
TMP = os.path.join(ROOT, "tmp")
SUPPORTED = {"esp32c6", "esp32c5", "esp32s3"}      # chips xcheck has a profile for


def roster():
    r = subprocess.run([sys.executable, DEV_PY, "fleet-status", "--json"],
                       capture_output=True, text=True, cwd=os.path.dirname(DEV_PY))
    return json.loads(r.stdout)


def identity(usb_path):
    """Read-only IDCODE probe — no halt, no DM. Safe on any TAP incl. production."""
    try:
        j = EspUsbJtag(usb_path)
        ic = j.read_idcode()
        usb.util.dispose_resources(j.dev)
        return ic, (chips.name_for(ic) or "??")
    except Exception as e:                               # noqa: BLE001
        return None, f"probe failed: {e}"


def usb_reset(name):
    subprocess.run([sys.executable, DEV_PY, "--target", name, "usb-reset"],
                   capture_output=True, text=True, cwd=os.path.dirname(DEV_PY))


def run_xcheck(name, chip, usb_path, serial):
    log = os.path.join(TMP, f"xcheck-{name}.log")
    r = subprocess.run([sys.executable, os.path.join(HERE, "xcheck.py"),
                        "--usb", usb_path, "--serial", serial, "--chip", chip],
                       capture_output=True, text=True)
    out = r.stdout + r.stderr
    with open(log, "w") as f:
        f.write(out)
    m = re.search(r"^RESULT: (.+)$", out, re.M)
    verdict = m.group(1).strip() if m else None
    mc = re.search(r"CORES targets=(\d+) harts=(\d+)", out)
    ncores = max(int(mc.group(1)), int(mc.group(2))) if mc else 0
    return verdict, ("words agree" in out), ncores, ("USBTimeoutError" in out
                                                     or "USBError" in out), log


def main():
    rows = []
    for d in roster():
        name, chip = d["name"], d["chip"]
        if not d["connected"]:
            rows.append((name, chip, "—", "absent", ""))
            continue
        usb_path, serial = d["usb_path"], d["serial"]
        if chip in SUPPORTED and not d["off_limits"]:
            print(f">> xcheck {name} [{chip}] usb={usb_path} ...", flush=True)
            verdict, memok, ncores, usberr, log = run_xcheck(name, chip, usb_path, serial)
            if verdict is None and usberr:               # wedged USB -> reset + retry
                print(f"   USB wedged; usb-reset + retry ...", flush=True)
                usb_reset(name)
                verdict, memok, ncores, usberr, log = run_xcheck(name, chip, usb_path, serial)
            res = (f"{verdict or 'NO VERDICT'}"
                   + ("  mem✓" if memok else "")
                   + (f"  cores={ncores}" if ncores else ""))
            rows.append((name, chip, usb_path, res, os.path.relpath(log, ROOT)))
        else:
            ic, nm = identity(usb_path)
            why = ("OFF-LIMITS prod — identity only" if d["off_limits"] else
                   f"{chip} unsupported by xcheck — identity only")
            ids = f"IDCODE={'0x%08x' % ic if ic is not None else 'n/a'} [{nm}]"
            rows.append((name, chip, usb_path, f"{ids}  ({why})", ""))

    print("\n" + "=" * 96)
    print(f"{'board':16} {'chip':9} {'usb_path':16} result")
    print("-" * 96)
    for name, chip, usb_path, result, log in rows:
        print(f"{name:16} {chip:9} {usb_path:16} {result}")
        if log:
            print(f"{'':43}{log}")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    sys.exit(main())
