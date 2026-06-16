#!/usr/bin/env python3
"""verify_chips_vs_tcl.py — prove chips.py's hand-transcribed register tables
against OpenOCD's VERBATIM cfg Tcl, hardware-free.

The C5 WDT bug existed because a table was hand-derived from soc headers while
openocd-esp32's esp32c5.cfg had the correct sequence verbatim. This check makes
that class of drift impossible to miss: for each RISC-V chip, extract the
`<chip>_wdt_disable` proc from the INSTALLED openocd-esp32 tree, execute it on
MiniJimTcl with a RECORDING mww backend (no hardware), and require the recorded
(addr, value) write set to equal what espjtag's chips.py wdt table would write.

Run by check.py --mock as a hardware-free gate step. Exit 1 on any drift.
"""
import glob
import re
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from mini_jimtcl import MiniJimTcl          # noqa: E402
from espjtag import chips                   # noqa: E402

OOCD_TARGETS = glob.glob(
    "/home/dan/.espressif/tools/openocd-esp32/*/openocd-esp32/share/openocd/"
    "scripts/target")
CHIP_CFGS = {"C6": "esp32c6.cfg", "C5": "esp32c5.cfg"}


def extract_proc(cfg_text, name):
    """Pull `proc <name> { } { ... }` out of a cfg by brace matching."""
    m = re.search(rf"proc\s+{re.escape(name)}\s*\{{[^}}]*\}}\s*\{{", cfg_text)
    if not m:
        return None
    depth, i = 1, m.end()
    while depth and i < len(cfg_text):
        depth += {"{": 1, "}": -1}.get(cfg_text[i], 0)
        i += 1
    return cfg_text[m.start():i]


def tcl_writes(proc_text, proc_name):
    """Execute the proc with a recording mww; return the written (addr, val) set."""
    writes = []
    t = MiniJimTcl()
    t.register("mww", lambda a: (writes.append((int(a[0], 0), int(a[1], 0))), "")[1])
    t.register("command", lambda a: "exec")      # `[command mode]` guard -> not config
    t.register("echo", lambda a: "")
    t.eval(proc_text)
    t.eval(proc_name)
    return writes


def table_writes(entry):
    """The write sequence espjtag's _wdt_disable performs from the chips table."""
    w = entry["wdt"]
    out = []
    for wkey, cfg, val in w["disable"]:
        out.append((wkey, w["key"]))
        out.append((cfg, val))
    out += [tuple(x) for x in w.get("int_clear", ())]
    return out


def main():
    if not OOCD_TARGETS:
        print("SKIP: no openocd-esp32 tree found"); return 0
    tdir = sorted(OOCD_TARGETS)[-1]
    fails = 0
    for name, cfg_file in CHIP_CFGS.items():
        entry = next(v for v in chips.CHIPS.values() if v.get("name") == name)
        cfg_text = open(f"{tdir}/{cfg_file}").read()
        proc_name = f"{cfg_file[:-4]}_wdt_disable"
        proc = extract_proc(cfg_text, proc_name)
        if proc is None:
            print(f"{name}: FAIL — {proc_name} not found in {cfg_file}")
            fails += 1
            continue
        ours, theirs = set(table_writes(entry)), set(tcl_writes(proc, proc_name))
        if ours == theirs:
            print(f"{name}: OK — chips.py wdt table == OpenOCD {proc_name} "
                  f"({len(theirs)} writes)")
        else:
            fails += 1
            print(f"{name}: DRIFT vs OpenOCD {proc_name}:")
            for a, v in sorted(theirs - ours):
                print(f"    OpenOCD writes (0x{a:08x}, 0x{v:08x}) — we don't")
            for a, v in sorted(ours - theirs):
                print(f"    we write     (0x{a:08x}, 0x{v:08x}) — OpenOCD doesn't")
    print("VERDICT:", "PASS" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
