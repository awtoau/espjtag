#!/usr/bin/env python3
"""test_xtensa_regs.py — NO-HARDWARE drift test: espjtag/xtensa_regs.py vs the
OpenOCD source of truth it was ported from.

Parses openocd-esp32's xtensa_regs.h (enum xtensa_reg_id) and xtensa.c
(xtensa_regs[] table) DIRECTLY from the pinned source tree and asserts our
transcription matches row-for-row: the index enum values (including the
ARLAST=64 jump), and every table row's name / reg_num / type / dbreg_num.

This is the leaf-level fidelity gate for the #29 register-cache port (per
docs/FAITHFUL-REIMPLEMENTATION.md "prove fidelity"). Run by check.py --mock.
Exit 1 on any drift. No chip, no mock transport needed — this leaf is pure data.
"""
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import espjtag.xtensa_regs as R                                  # noqa: E402

# Pinned source tree (mirror). Skips cleanly if absent (e.g. CI without it).
SRC_CANDIDATES = [
    "/mnt/2tb/git_mirror/espressif/openocd-esp32/src/target/xtensa",
] + glob.glob("/home/dan/.espressif/tools/openocd-esp32/*/openocd-esp32/"
              "src/target/xtensa")

_fail = 0


def check(label, got, want):
    global _fail
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
          + ("" if ok else f"\n      got  {got}\n      want {want}"))
    _fail |= (not ok)


def parse_index_enum(hdr):
    """enum xtensa_reg_id { ... } -> {name: value}, honoring `= N` resets."""
    m = re.search(r"enum xtensa_reg_id\s*\{(.*?)\}", hdr, re.S)
    body = m.group(1)
    out, nxt = {}, 0
    for raw in body.split(","):
        line = re.sub(r"/\*.*?\*/", "", raw, flags=re.S).strip()
        if not line:
            continue
        mm = re.match(r"(\w+)\s*(?:=\s*(\w+))?$", line)
        if not mm:
            continue
        name, val = mm.group(1), mm.group(2)
        if val is not None:
            nxt = out[val] if val in out else int(val, 0)
        out[name] = nxt
        nxt += 1
    return out


# C type token -> our module constant
_TYPE = {
    "XT_REG_GENERAL": R.XT_REG_GENERAL, "XT_REG_USER": R.XT_REG_USER,
    "XT_REG_SPECIAL": R.XT_REG_SPECIAL, "XT_REG_DEBUG": R.XT_REG_DEBUG,
    "XT_REG_RELGEN": R.XT_REG_RELGEN, "XT_REG_FR": R.XT_REG_FR,
    "XT_REG_TIE": R.XT_REG_TIE, "XT_REG_OTHER": R.XT_REG_OTHER,
}


def parse_table(src, idx_enum):
    """xtensa_regs[] -> list of (name, reg_num, type). Resolves the symbolic
    reg_nums and the XT_REG_IDX_* used as RELGEN reg_nums."""
    m = re.search(r"struct xtensa_reg_desc xtensa_regs\[XT_NUM_REGS\]\s*=\s*\{(.*?)\n\};",
                  src, re.S)
    body = m.group(1)
    syms = {"XT_PC_REG_NUM_VIRTUAL": R.XT_PC_REG_NUM_VIRTUAL,
            "XT_PS_REG_NUM": R.XT_PS_REG_NUM}
    rows = []
    for mm in re.finditer(r'XT_MK_REG_DESC\("([^"]+)",\s*([^,]+),\s*(XT_REG_\w+),\s*([^)]+)\)',
                          body):
        name, rnum_tok, typ_tok = mm.group(1), mm.group(2).strip(), mm.group(3)
        if rnum_tok in syms:
            rnum = syms[rnum_tok]
        elif rnum_tok in idx_enum:
            rnum = idx_enum[rnum_tok]
        else:
            rnum = int(rnum_tok, 0)
        rows.append((name, rnum, _TYPE[typ_tok]))
    return rows


def main():
    src_dir = next((d for d in SRC_CANDIDATES if os.path.isdir(d)), None)
    if not src_dir:
        print("SKIP: no openocd-esp32 source tree found"); return 0
    print(f"=== xtensa_regs.py vs OpenOCD source ({src_dir}) — NO HARDWARE ===")
    hdr = open(f"{src_dir}/xtensa_regs.h").read()
    src = open(f"{src_dir}/xtensa.c").read()

    # 1) index enum
    idx = parse_index_enum(hdr)
    check("XT_NUM_REGS", R.XT_NUM_REGS, idx["XT_NUM_REGS"])
    check("XT_REG_IDX_ARLAST == 64", R.XT_REG_IDX_ARLAST, idx["XT_REG_IDX_ARLAST"])
    for nm in ("XT_REG_IDX_PC", "XT_REG_IDX_WINDOWBASE", "XT_REG_IDX_PS",
               "XT_REG_IDX_DDR", "XT_REG_IDX_DEBUGCAUSE", "XT_REG_IDX_A0",
               "XT_REG_IDX_A15"):
        check(f"{nm} == {idx[nm]}", getattr(R, nm), idx[nm])

    # 2) the full table, row for row
    rows = parse_table(src, idx)
    check("table length", len(R.xtensa_regs), len(rows))
    drift = 0
    for i, (name, rnum, typ) in enumerate(rows):
        d = R.xtensa_regs[i]
        want_db = R._xt_mk_dbregn(rnum, typ)
        if (d.name, d.reg_num, d.type, d.dbreg_num) != (name, rnum, typ, want_db):
            drift += 1
            print(f"  [FAIL] row {i}: ours=({d.name},0x{d.reg_num:x},t{d.type},"
                  f"db0x{d.dbreg_num:x}) src=({name},0x{rnum:x},t{typ},db0x{want_db:x})")
    check("all 97 table rows match (name/reg_num/type/dbreg_num)", drift, 0)

    print("VERDICT:", "PASS" if not _fail else "FAIL")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
