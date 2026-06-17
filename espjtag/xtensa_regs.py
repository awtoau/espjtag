"""espjtag.xtensa_regs — VERBATIM port of OpenOCD's Xtensa register-descriptor
model, the foundation of the faithful register-cache (#29).

Ported 1:1 from openocd-esp32 @ f10eceff22fb8dcd3db69bf3ebc5c70602454af6:
  * src/target/xtensa/xtensa_regs.h  — enum xtensa_reg_id, enum xtensa_reg_type,
    enum xtensa_reg_flags, struct xtensa_reg_desc, _XT_MK_DBREGN / XT_MK_REG_DESC.
  * src/target/xtensa/xtensa.c:166-169 — XT_PS_REG_NUM / XT_EPC_REG_NUM_BASE /
    XT_PC_REG_NUM_VIRTUAL.
  * src/target/xtensa/xtensa.c:178-280 — the xtensa_regs[] descriptor table.

Per docs/FAITHFUL-REIMPLEMENTATION.md: transcribed, not re-derived. The index enum
is copied byte-for-byte INCLUDING the explicit `XT_REG_IDX_ARLAST = 64` jump — a
re-derived sequential numbering puts WINDOWBASE..A15 ~47 positions wrong (the bug
the fidelity check caught). The reg-type values are plain module constants, NOT a
Python IntEnum: IntEnum dedups equal values into aliases and loses `.name`, so
RELGEN_VAL(0x0000) would alias GENERAL(0), DEBUG_VAL would alias SPECIAL_VAL, etc.
"""

# --- xtensa.c:166-169 (symbolic reg_num markers) ----------------------------
XT_PS_REG_NUM = 0xE6
XT_EPC_REG_NUM_BASE = 0xB0          # (EPC1 - 1), for adding DBGLEVEL
XT_PC_REG_NUM_VIRTUAL = 0xFF        # Marker for computing PC (EPC[DBGLEVEL])

# --- xtensa_regs.h:15-68 — enum xtensa_reg_id (VERBATIM; note ARLAST = 64) ---
# The values are the C enumerator values exactly. PC=0, AR0..AR15 = 1..16, then
# the enum SKIPS to ARLAST=64 (max 64 ARs), so WINDOWBASE=65, ..., A0..A15 =
# 81..96, XT_NUM_REGS=97. Do NOT renumber these.
XT_REG_IDX_PC = 0
XT_REG_IDX_AR0 = 1
XT_REG_IDX_ARFIRST = XT_REG_IDX_AR0
XT_REG_IDX_AR1 = 2
XT_REG_IDX_AR2 = 3
XT_REG_IDX_AR3 = 4
XT_REG_IDX_AR4 = 5
XT_REG_IDX_AR5 = 6
XT_REG_IDX_AR6 = 7
XT_REG_IDX_AR7 = 8
XT_REG_IDX_AR8 = 9
XT_REG_IDX_AR9 = 10
XT_REG_IDX_AR10 = 11
XT_REG_IDX_AR11 = 12
XT_REG_IDX_AR12 = 13
XT_REG_IDX_AR13 = 14
XT_REG_IDX_AR14 = 15
XT_REG_IDX_AR15 = 16
XT_REG_IDX_ARLAST = 64              # Max 64 ARs
XT_REG_IDX_WINDOWBASE = 65
XT_REG_IDX_WINDOWSTART = 66
XT_REG_IDX_PS = 67
XT_REG_IDX_IBREAKENABLE = 68
XT_REG_IDX_DDR = 69
XT_REG_IDX_IBREAKA0 = 70
XT_REG_IDX_IBREAKA1 = 71
XT_REG_IDX_DBREAKA0 = 72
XT_REG_IDX_DBREAKA1 = 73
XT_REG_IDX_DBREAKC0 = 74
XT_REG_IDX_DBREAKC1 = 75
XT_REG_IDX_CPENABLE = 76
XT_REG_IDX_EXCCAUSE = 77
XT_REG_IDX_DEBUGCAUSE = 78
XT_REG_IDX_ICOUNT = 79
XT_REG_IDX_ICOUNTLEVEL = 80
XT_REG_IDX_A0 = 81
XT_REG_IDX_A1 = 82
XT_REG_IDX_A2 = 83
XT_REG_IDX_A3 = 84
XT_REG_IDX_A4 = 85
XT_REG_IDX_A5 = 86
XT_REG_IDX_A6 = 87
XT_REG_IDX_A7 = 88
XT_REG_IDX_A8 = 89
XT_REG_IDX_A9 = 90
XT_REG_IDX_A10 = 91
XT_REG_IDX_A11 = 92
XT_REG_IDX_A12 = 93
XT_REG_IDX_A13 = 94
XT_REG_IDX_A14 = 95
XT_REG_IDX_A15 = 96
XT_NUM_REGS = 97

XT_NUM_A_REGS = 16                  # xtensa_regs.h:72

# --- xtensa_regs.h:74-105 — enum xtensa_reg_type (plain constants, NOT IntEnum) -
# Type tags 0..7 + the _VAL/_MASK encodings. Kept as distinct module constants so
# every C enumerator retains its identity (an IntEnum would alias the equal values).
XT_REG_GENERAL = 0      # General-purpose register; part of the windowed register set
XT_REG_USER = 1         # User register, needs RUR to read
XT_REG_SPECIAL = 2      # Special register, needs RSR to read
XT_REG_DEBUG = 3        # Register used for the debug interface. Don't mess with this.
XT_REG_RELGEN = 4       # Relative general address (abs addresses plus the window index)
XT_REG_FR = 5           # Floating-point register
XT_REG_TIE = 6          # TIE (custom) register
XT_REG_OTHER = 7        # Other (typically legacy) register
XT_REG_TYPE_NUM = 8

XT_REG_GENERAL_MASK = 0xFFC0
XT_REG_GENERAL_VAL = 0x0100
XT_REG_USER_MASK = 0xFF00
XT_REG_USER_VAL = 0x0300
XT_REG_SPECIAL_MASK = 0xFF00
XT_REG_SPECIAL_VAL = 0x0200
XT_REG_DEBUG_MASK = 0xFF00
XT_REG_DEBUG_VAL = 0x0200
XT_REG_RELGEN_MASK = 0xFFE0
XT_REG_RELGEN_VAL = 0x0000
XT_REG_FR_MASK = 0xFFF0
XT_REG_FR_VAL = 0x0030
XT_REG_TIE_MASK = 0xFFF0
XT_REG_TIE_VAL = 0x1000
XT_REG_OTHER_MASK = 0xFFFF
XT_REG_OTHER_VAL = 0xF000           # unused
XT_REG_INDEX_MASK = 0x00FF

# per-type _VAL, for _XT_MK_DBREGN (dbreg_num = type##_VAL | reg_num)
_TYPE_VAL = {
    XT_REG_GENERAL: XT_REG_GENERAL_VAL,
    XT_REG_USER: XT_REG_USER_VAL,
    XT_REG_SPECIAL: XT_REG_SPECIAL_VAL,
    XT_REG_DEBUG: XT_REG_DEBUG_VAL,
    XT_REG_RELGEN: XT_REG_RELGEN_VAL,
    XT_REG_FR: XT_REG_FR_VAL,
    XT_REG_TIE: XT_REG_TIE_VAL,
    XT_REG_OTHER: XT_REG_OTHER_VAL,
}

# --- xtensa_regs.h:107-111 — enum xtensa_reg_flags ---------------------------
XT_REGF_NOREAD = 0x01               # Register is write-only
XT_REGF_COPROC0 = 0x02              # Can't be read if coproc0 isn't enabled
XT_REGF_MASK = 0x03


# --- xtensa_regs.h:113-131 — struct xtensa_reg_desc + the dbreg/mk macros -----
def _xt_mk_dbregn(reg_num, reg_type):
    """_XT_MK_DBREGN(reg_num, reg_type) ((reg_type##_VAL) | (reg_num))."""
    return _TYPE_VAL[reg_type] | reg_num


class XtensaRegDesc:
    """struct xtensa_reg_desc { name; exist; reg_num; dbreg_num; type; flags }."""

    __slots__ = ("name", "exist", "reg_num", "dbreg_num", "type", "flags")

    def __init__(self, name, reg_num, reg_type, flags):
        # XT_MK_REG_DESC(n, r, t, f): exist=false, dbreg_num = _XT_MK_DBREGN(r, t)
        self.name = name
        self.exist = False
        self.reg_num = reg_num
        self.dbreg_num = _xt_mk_dbregn(reg_num, reg_type)
        self.type = reg_type
        self.flags = flags

    def __repr__(self):
        return f"XtensaRegDesc({self.name!r}, reg_num=0x{self.reg_num:x}, type={self.type})"


def _D(n, r, t, f):
    return XtensaRegDesc(n, r, t, f)


# --- xtensa.c:178-280 — xtensa_regs[XT_NUM_REGS] (the 97-row table, VERBATIM) --
xtensa_regs = [
    _D("pc", XT_PC_REG_NUM_VIRTUAL, XT_REG_SPECIAL, 0),
    _D("ar0", 0x00, XT_REG_GENERAL, 0),
    _D("ar1", 0x01, XT_REG_GENERAL, 0),
    _D("ar2", 0x02, XT_REG_GENERAL, 0),
    _D("ar3", 0x03, XT_REG_GENERAL, 0),
    _D("ar4", 0x04, XT_REG_GENERAL, 0),
    _D("ar5", 0x05, XT_REG_GENERAL, 0),
    _D("ar6", 0x06, XT_REG_GENERAL, 0),
    _D("ar7", 0x07, XT_REG_GENERAL, 0),
    _D("ar8", 0x08, XT_REG_GENERAL, 0),
    _D("ar9", 0x09, XT_REG_GENERAL, 0),
    _D("ar10", 0x0A, XT_REG_GENERAL, 0),
    _D("ar11", 0x0B, XT_REG_GENERAL, 0),
    _D("ar12", 0x0C, XT_REG_GENERAL, 0),
    _D("ar13", 0x0D, XT_REG_GENERAL, 0),
    _D("ar14", 0x0E, XT_REG_GENERAL, 0),
    _D("ar15", 0x0F, XT_REG_GENERAL, 0),
    _D("ar16", 0x10, XT_REG_GENERAL, 0),
    _D("ar17", 0x11, XT_REG_GENERAL, 0),
    _D("ar18", 0x12, XT_REG_GENERAL, 0),
    _D("ar19", 0x13, XT_REG_GENERAL, 0),
    _D("ar20", 0x14, XT_REG_GENERAL, 0),
    _D("ar21", 0x15, XT_REG_GENERAL, 0),
    _D("ar22", 0x16, XT_REG_GENERAL, 0),
    _D("ar23", 0x17, XT_REG_GENERAL, 0),
    _D("ar24", 0x18, XT_REG_GENERAL, 0),
    _D("ar25", 0x19, XT_REG_GENERAL, 0),
    _D("ar26", 0x1A, XT_REG_GENERAL, 0),
    _D("ar27", 0x1B, XT_REG_GENERAL, 0),
    _D("ar28", 0x1C, XT_REG_GENERAL, 0),
    _D("ar29", 0x1D, XT_REG_GENERAL, 0),
    _D("ar30", 0x1E, XT_REG_GENERAL, 0),
    _D("ar31", 0x1F, XT_REG_GENERAL, 0),
    _D("ar32", 0x20, XT_REG_GENERAL, 0),
    _D("ar33", 0x21, XT_REG_GENERAL, 0),
    _D("ar34", 0x22, XT_REG_GENERAL, 0),
    _D("ar35", 0x23, XT_REG_GENERAL, 0),
    _D("ar36", 0x24, XT_REG_GENERAL, 0),
    _D("ar37", 0x25, XT_REG_GENERAL, 0),
    _D("ar38", 0x26, XT_REG_GENERAL, 0),
    _D("ar39", 0x27, XT_REG_GENERAL, 0),
    _D("ar40", 0x28, XT_REG_GENERAL, 0),
    _D("ar41", 0x29, XT_REG_GENERAL, 0),
    _D("ar42", 0x2A, XT_REG_GENERAL, 0),
    _D("ar43", 0x2B, XT_REG_GENERAL, 0),
    _D("ar44", 0x2C, XT_REG_GENERAL, 0),
    _D("ar45", 0x2D, XT_REG_GENERAL, 0),
    _D("ar46", 0x2E, XT_REG_GENERAL, 0),
    _D("ar47", 0x2F, XT_REG_GENERAL, 0),
    _D("ar48", 0x30, XT_REG_GENERAL, 0),
    _D("ar49", 0x31, XT_REG_GENERAL, 0),
    _D("ar50", 0x32, XT_REG_GENERAL, 0),
    _D("ar51", 0x33, XT_REG_GENERAL, 0),
    _D("ar52", 0x34, XT_REG_GENERAL, 0),
    _D("ar53", 0x35, XT_REG_GENERAL, 0),
    _D("ar54", 0x36, XT_REG_GENERAL, 0),
    _D("ar55", 0x37, XT_REG_GENERAL, 0),
    _D("ar56", 0x38, XT_REG_GENERAL, 0),
    _D("ar57", 0x39, XT_REG_GENERAL, 0),
    _D("ar58", 0x3A, XT_REG_GENERAL, 0),
    _D("ar59", 0x3B, XT_REG_GENERAL, 0),
    _D("ar60", 0x3C, XT_REG_GENERAL, 0),
    _D("ar61", 0x3D, XT_REG_GENERAL, 0),
    _D("ar62", 0x3E, XT_REG_GENERAL, 0),
    _D("ar63", 0x3F, XT_REG_GENERAL, 0),
    _D("windowbase", 0x48, XT_REG_SPECIAL, 0),
    _D("windowstart", 0x49, XT_REG_SPECIAL, 0),
    _D("ps", XT_PS_REG_NUM, XT_REG_SPECIAL, 0),   # PS (not mapped through EPS[])
    _D("ibreakenable", 0x60, XT_REG_SPECIAL, 0),
    _D("ddr", 0x68, XT_REG_DEBUG, XT_REGF_NOREAD),
    _D("ibreaka0", 0x80, XT_REG_SPECIAL, 0),
    _D("ibreaka1", 0x81, XT_REG_SPECIAL, 0),
    _D("dbreaka0", 0x90, XT_REG_SPECIAL, 0),
    _D("dbreaka1", 0x91, XT_REG_SPECIAL, 0),
    _D("dbreakc0", 0xA0, XT_REG_SPECIAL, 0),
    _D("dbreakc1", 0xA1, XT_REG_SPECIAL, 0),
    _D("cpenable", 0xE0, XT_REG_SPECIAL, 0),
    _D("exccause", 0xE8, XT_REG_SPECIAL, 0),
    _D("debugcause", 0xE9, XT_REG_SPECIAL, 0),
    _D("icount", 0xEC, XT_REG_SPECIAL, 0),
    _D("icountlevel", 0xED, XT_REG_SPECIAL, 0),

    # WARNING: For these registers, regnum points to the index of the
    # corresponding ARx registers, NOT to the processor register number!
    _D("a0", XT_REG_IDX_AR0, XT_REG_RELGEN, 0),
    _D("a1", XT_REG_IDX_AR1, XT_REG_RELGEN, 0),
    _D("a2", XT_REG_IDX_AR2, XT_REG_RELGEN, 0),
    _D("a3", XT_REG_IDX_AR3, XT_REG_RELGEN, 0),
    _D("a4", XT_REG_IDX_AR4, XT_REG_RELGEN, 0),
    _D("a5", XT_REG_IDX_AR5, XT_REG_RELGEN, 0),
    _D("a6", XT_REG_IDX_AR6, XT_REG_RELGEN, 0),
    _D("a7", XT_REG_IDX_AR7, XT_REG_RELGEN, 0),
    _D("a8", XT_REG_IDX_AR8, XT_REG_RELGEN, 0),
    _D("a9", XT_REG_IDX_AR9, XT_REG_RELGEN, 0),
    _D("a10", XT_REG_IDX_AR10, XT_REG_RELGEN, 0),
    _D("a11", XT_REG_IDX_AR11, XT_REG_RELGEN, 0),
    _D("a12", XT_REG_IDX_AR12, XT_REG_RELGEN, 0),
    _D("a13", XT_REG_IDX_AR13, XT_REG_RELGEN, 0),
    _D("a14", XT_REG_IDX_AR14, XT_REG_RELGEN, 0),
    _D("a15", XT_REG_IDX_AR15, XT_REG_RELGEN, 0),
]

# Fidelity tripwire: the table is indexed BY the enum, so its length must equal
# XT_NUM_REGS and row[idx].name must match the enum name. Catches a transcription
# slip (a dropped/added row) loudly at import.
assert len(xtensa_regs) == XT_NUM_REGS, (len(xtensa_regs), XT_NUM_REGS)
assert xtensa_regs[XT_REG_IDX_PC].name == "pc"
assert xtensa_regs[XT_REG_IDX_DDR].name == "ddr"
assert xtensa_regs[XT_REG_IDX_DEBUGCAUSE].name == "debugcause"
assert xtensa_regs[XT_REG_IDX_A0].name == "a0"
assert xtensa_regs[XT_REG_IDX_WINDOWBASE].name == "windowbase"
