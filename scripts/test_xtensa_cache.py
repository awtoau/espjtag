#!/usr/bin/env python3
"""test_xtensa_cache.py — NO-HARDWARE test of the Xtensa register CACHE
(espjtag/xtensa_cache.py), the leaf the resume/start_algorithm port builds on.

Asserts the verbatim OpenOCD semantics (xtensa.c:1031-1040,1129-1143,538-567):
  * reg_get reads without dirtying; reg_set dirties ONLY on a value change.
  * reg_set_value sets .dirty, NOT .valid (the docstring-claim bug the fidelity
    check flagged).
  * windowbase_offset_to_canonical: LX stride 4, masks by aregs_num-1, and is the
    exact inverse of canonical_to_windowbase_offset.
No chip, no mock transport — the cache is pure host state. Run by check.py --mock.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import espjtag.xtensa_regs as R                                  # noqa: E402
import espjtag.xtensa_cache as C                                 # noqa: E402

_fail = 0


def check(label, got, want):
    global _fail
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
          + ("" if ok else f"\n      got  {got!r}\n      want {want!r}"))
    _fail |= (not ok)


def test_get_set_dirty():
    cache = C.XtensaRegCache()
    # fresh: not dirty, value 0
    check("fresh A2 not dirty", cache.reg_list[R.XT_REG_IDX_A2].dirty, False)
    check("fresh A2 value 0", C.xtensa_reg_get(cache, R.XT_REG_IDX_A2), 0)
    # set to a new value -> dirty
    C.xtensa_reg_set(cache, R.XT_REG_IDX_A2, 0xDEADBEEF)
    check("set A2 new value reads back", C.xtensa_reg_get(cache, R.XT_REG_IDX_A2), 0xDEADBEEF)
    check("set A2 new value dirties", cache.reg_list[R.XT_REG_IDX_A2].dirty, True)
    # reg_set to the SAME value must NOT dirty (the early-return in xtensa_reg_set)
    cache.reg_list[R.XT_REG_IDX_A3].dirty = False
    cache.reg_list[R.XT_REG_IDX_A3].value = 0x1234
    C.xtensa_reg_set(cache, R.XT_REG_IDX_A3, 0x1234)
    check("reg_set same value does NOT dirty", cache.reg_list[R.XT_REG_IDX_A3].dirty, False)


def test_set_value_dirty_not_valid():
    cache = C.XtensaRegCache()
    reg = cache.reg_list[R.XT_REG_IDX_PS]
    reg.dirty = reg.valid = False
    C.xtensa_reg_set_value(reg, 0x60025)
    check("set_value sets .dirty", reg.dirty, True)
    check("set_value does NOT set .valid", reg.valid, False)   # the flagged bug


def test_get_does_not_dirty():
    cache = C.XtensaRegCache()
    cache.reg_list[R.XT_REG_IDX_A5].value = 0xAA
    cache.reg_list[R.XT_REG_IDX_A5].dirty = False
    _ = C.xtensa_reg_get(cache, R.XT_REG_IDX_A5)
    check("reg_get does not dirty", cache.reg_list[R.XT_REG_IDX_A5].dirty, False)


def test_windowbase_canonical():
    cache = C.XtensaRegCache()           # LX, aregs_num 64, stride 4
    # windowbase 0 is identity for the AR block
    check("wb=0 AR0 -> AR0",
          C.xtensa_windowbase_offset_to_canonical(cache, R.XT_REG_IDX_AR0, 0),
          R.XT_REG_IDX_AR0)
    # a-reg A0 maps into the AR block; wb=1 (stride 4) shifts by 4
    got = C.xtensa_windowbase_offset_to_canonical(cache, R.XT_REG_IDX_A0, 1)
    check("wb=1 A0 -> AR4", got, R.XT_REG_IDX_AR0 + 4)
    # wrap: AR0 at wb=16 (16*4=64) wraps mod 64 back to AR0
    check("wb=16 AR0 wraps to AR0 (mod 64)",
          C.xtensa_windowbase_offset_to_canonical(cache, R.XT_REG_IDX_AR0, 16),
          R.XT_REG_IDX_AR0)
    # inverse round-trip: canonical_to_windowbase is offset_to_canonical(-wb)
    for wb in (0, 1, 3, 7):
        fwd = C.xtensa_windowbase_offset_to_canonical(cache, R.XT_REG_IDX_AR8, wb)
        back = C.xtensa_canonical_to_windowbase_offset(cache, fwd, wb)
        check(f"round-trip AR8 @ wb={wb}", back, R.XT_REG_IDX_AR8)


def test_cause_get_clear_lx():
    """LX (the S3): cause_get reads cached DEBUGCAUSE; cause_clear zeroes it and
    clears dirty. The XT_LX=1 value is the #29 REDO fix (was wrongly 0)."""
    check("XT_LX == 1 (was wrongly 0)", C.XT_LX, 1)
    check("XT_NX == 2", C.XT_NX, 2)
    cache = C.XtensaRegCache()           # LX
    # simulate a halt-on-BREAK: DEBUGCAUSE has BI set
    cache.reg_list[R.XT_REG_IDX_DEBUGCAUSE].value = C.DEBUGCAUSE_BI
    cause = C.xtensa_cause_get(cache)
    check("cause_get returns cached DEBUGCAUSE (BI set)", cause & C.DEBUGCAUSE_BI, C.DEBUGCAUSE_BI)
    check("DEBUGCAUSE_BI is BIT(3)", C.DEBUGCAUSE_BI, 1 << 3)
    check("DEBUGCAUSE_BN is BIT(4)", C.DEBUGCAUSE_BN, 1 << 4)
    check("DEBUGCAUSE_DB is BIT(2)", C.DEBUGCAUSE_DB, 1 << 2)
    # clear
    C.xtensa_cause_clear(cache)
    check("cause_clear zeroes DEBUGCAUSE", C.xtensa_cause_get(cache), 0)
    check("cause_clear leaves DEBUGCAUSE not dirty",
          cache.reg_list[R.XT_REG_IDX_DEBUGCAUSE].dirty, False)


def main():
    print("=== Xtensa register cache — NO HARDWARE (host model) ===")
    test_get_set_dirty()
    test_set_value_dirty_not_valid()
    test_get_does_not_dirty()
    test_windowbase_canonical()
    test_cause_get_clear_lx()
    print("  (no hardware was touched)")
    print("VERDICT:", "PASS" if not _fail else "FAIL")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
