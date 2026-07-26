#!/usr/bin/env python3
"""check.py — THE post-change regression gate, in two modes.

    python3 scripts/check.py --mock    # NO HARDWARE: hardware-free tests only
                                        # (CI-able, fast, deterministic).
    python3 scripts/check.py --real    # ON-TARGET: tests that need a chip.
    python3 scripts/check.py           # both (mock first, then real). Default.

MOCK steps (no chip):
  - tcl-drift                     (chips.py vs OpenOCD Tcl, hardware-free)
  - S3 flasher (software model)   (test_s3_mock.tcl via the Tcl bridge)
REAL steps (need a chip):
  - selftest + DMIRESET C6/C5     (drain_mode=validate byte accounting)
  - xcheck 3-way JTAG dump        (espjtag vs OpenOCD vs probe-rs)
  - S3 flasher (on target)        (test_s3_stub.tcl)
  - reset guard on the S3 (#51)   (Xtensa reset paths refuse; non-destructive)
  - flash bench, recorded         (-> tmp/flash-bench.db keyed by git sha)
  - progression graph             (-> docs/images/flash-progression.png)

Boards default to the bench fixtures; override via env (C6_USB, C5_USB, S3_MAC,
C6_MAC, C6_TTY, FLASHERS, NOTE) for other setups. Full output -> tmp/check.log.
Replaces the old bash check-all.sh (no shell scripts — repo rule).
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")
if not os.path.exists(PY):
    PY = sys.executable
LOG = os.path.join(ROOT, "tmp", "check.log")

ENV = os.environ
NOTE = ENV.get("NOTE", "check")
C6_USB = ENV.get("C6_USB", "1-1.3.1.3.1")          # c6-xiao-a
C6_MAC = ENV.get("C6_MAC", "E4:B0:63:41:C1:44")
C6_TTY = ENV.get("C6_TTY", "/dev/ttyACM2")
C5_USB = ENV.get("C5_USB", "1-1.2")                # c5-xiao-a
S3_MAC = ENV.get("S3_MAC", "1C:DB:D4:76:82:08")    # s3-xiao-sense (#29 flasher; serial-pinned, stable across re-enum)
FLASHERS = ENV.get("FLASHERS", "espjtag-full,espjtag-incr,esptool-incr")


def _s(*a):
    return [os.path.join(ROOT, "scripts", a[0])] + list(a[1:])


# (name, argv-after-python). Boards/flags resolved above.
MOCK_STEPS = [
    # (invariant test moved out with the CubeProgrammer docs — stlinkprogrammer repo)
    ("tcl-drift (hw-free)", _s("verify_chips_vs_tcl.py")),
    ("xtensa-regs drift (hw-free)", _s("test_xtensa_regs.py")),
    ("xtensa-cache (hw-free)", _s("test_xtensa_cache.py")),
    ("xtensa-algorithm (hw-free)", _s("test_xtensa_algorithm.py")),
    ("s3-flasher (model)", _s("ocd_tcl_bridge.py", "--mock",
                              "--tcl", os.path.join(ROOT, "scripts", "test_s3_mock.tcl"))),
    ("riscv-dmi (model)", _s("test_riscv_mock.py")),
]
REAL_STEPS = [
    ("selftest+dmireset C6", _s("validate_dmireset.py", "--usb-path", C6_USB)),
    ("selftest+dmireset C5", _s("validate_dmireset.py", "--usb-path", C5_USB)),
    ("xcheck 3-way dump C6", _s("xcheck.py", "--usb", C6_USB, "--serial", C6_MAC)),
    ("s3-flasher (target)", _s("ocd_tcl_bridge.py", "--serial", S3_MAC,
                               "--tcl", os.path.join(ROOT, "scripts", "test_s3_stub.tcl"))),
    ("reset-guard S3 (#51)", _s("test_reset_guard_hw.py", "--serial", S3_MAC)),
    ("flash bench (record)", _s("flash_bench.py", "--usb", C6_USB, "--tty", C6_TTY,
                                "--rounds", "3", "--flashers", FLASHERS,
                                "--record", "--note", NOTE)),
    ("progression graph", _s("flash_bench.py", "--graph")),
]


def run_step(name, argv, logf):
    print(f"=== {name} ===")
    logf.write(f"\n=== {name} ===\n")
    logf.flush()
    rc = subprocess.run([PY] + argv, stdout=logf, stderr=subprocess.STDOUT).returncode
    print("  PASS" if rc == 0 else f"  FAIL (rc={rc}) — see {LOG}")
    return rc


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--mock", action="store_true", help="no-hardware tests only")
    g.add_argument("--real", action="store_true", help="on-target tests only")
    args = ap.parse_args()
    mode = "mock" if args.mock else "real" if args.real else "all"

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    results = []
    with open(LOG, "w") as logf:
        if mode in ("mock", "all"):
            print("--- MOCK (no hardware) ---")
            for name, argv in MOCK_STEPS:
                results.append((name, run_step(name, argv, logf)))
        if mode in ("real", "all"):
            print("--- REAL (on target) ---")
            for name, argv in REAL_STEPS:
                results.append((name, run_step(name, argv, logf)))

    print(f"=== check summary ({mode}; note: {NOTE}) ===")
    fail = 0
    for name, rc in results:
        print(f"  {name:22s} {'PASS' if rc == 0 else 'FAIL'}")
        fail |= (rc != 0)
    return fail


if __name__ == "__main__":
    sys.exit(main())
