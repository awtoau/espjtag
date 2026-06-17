"""espjtag.xtensa_cache — VERBATIM port of OpenOCD's Xtensa register CACHE and the
cache-only helpers, built on xtensa_regs.py. This is the host-side model of the
core registers (value/dirty/valid per register) that resume/start_algorithm
operate on; it touches NO hardware.

Ported 1:1 from openocd-esp32 @ f10eceff22fb8dcd3db69bf3ebc5c70602454af6:
  * xtensa.c:1031-1040 — xtensa_reg_get_value / xtensa_reg_set_value
  * xtensa.c:1129-1143 — xtensa_reg_get / xtensa_reg_set
  * xtensa.c:538-554   — xtensa_windowbase_offset_to_canonical
  * xtensa.c:556-561   — xtensa_canonical_to_windowbase_offset
  * xtensa.c:563-567   — xtensa_mark_register_dirty

Per docs/FAITHFUL-REIMPLEMENTATION.md. DISCLOSED DEVIATIONS (kept minimal):
  * C `struct reg.value` is a uint8_t[size/8] LE byte buffer; we store `.value`
    as a Python uint32 and `*_value` get/set are plain reads/writes. Value-
    preserving for the 32-bit registers this port uses (the only case exercised).
  * `XT_LX` core-type defaults true; the NX windowbase stride (8) and aregs_num
    are still ported, but the S3 target is LX (stride 4, aregs_num 64).
"""

from . import xtensa_regs as R

XT_LX = 1                       # enum xtensa_type { XT_UNDEF=0, XT_LX=1, XT_NX=2 }
XT_NX = 2                       # (xtensa.h:105-109) — corrected per the #29 REDO

# S3 = Xtensa LX7: 64 physical ARs, windowbase stride 4. (core_config equivalents)
AREGS_NUM = 64


class Reg:
    """struct reg (the fields the cache model uses): value/dirty/valid/exist/
    name/size. `size` is in BITS (C reg->size); 32 for every Xtensa core reg here."""

    __slots__ = ("name", "value", "dirty", "valid", "exist", "size")

    def __init__(self, desc):
        self.name = desc.name
        self.value = 0          # uint32 (C: uint8_t[4] LE buffer — see module note)
        self.dirty = False
        self.valid = False
        self.exist = desc.exist
        self.size = 32


class XtensaRegCache:
    """xtensa->core_cache: reg_list[XT_NUM_REGS] indexed by XT_REG_IDX_*, plus the
    core-type/aregs config the windowbase math needs. A pure host model."""

    def __init__(self, core_type=XT_LX, aregs_num=AREGS_NUM):
        self.reg_list = [Reg(d) for d in R.xtensa_regs]
        self.num_regs = R.XT_NUM_REGS
        self.core_type = core_type
        self.aregs_num = aregs_num


# --- xtensa.c:1031-1040 — value get/set on a single reg ----------------------
def xtensa_reg_get_value(reg):
    """buf_get_u32(reg->value, 0, 32)."""
    return reg.value & 0xFFFFFFFF


def xtensa_reg_set_value(reg, value):
    """buf_set_u32(reg->value, 0, 32, value); reg->dirty = true."""
    reg.value = value & 0xFFFFFFFF
    reg.dirty = True


# --- xtensa.c:1129-1143 — cache get/set by index -----------------------------
def xtensa_reg_get(cache, reg_id):
    """reg = &core_cache->reg_list[reg_id]; return xtensa_reg_get_value(reg)."""
    reg = cache.reg_list[reg_id]
    return xtensa_reg_get_value(reg)


def xtensa_reg_set(cache, reg_id, value):
    """reg = &core_cache->reg_list[reg_id]; if get_value(reg)==value: return;
    xtensa_reg_set_value(reg, value)."""
    reg = cache.reg_list[reg_id]
    if xtensa_reg_get_value(reg) == (value & 0xFFFFFFFF):
        return
    xtensa_reg_set_value(reg, value)


# --- xtensa.c:563-567 — mark a cached register dirty -------------------------
def xtensa_mark_register_dirty(cache, reg_idx):
    """reg_list[reg_idx].dirty = true."""
    cache.reg_list[reg_idx].dirty = True


# --- xtensa.c:538-561 — windowbase offset <-> canonical ----------------------
def xtensa_windowbase_offset_to_canonical(cache, reg_idx, windowbase):
    """Convert a register index indexed relative to windowbase to the real addr."""
    if R.XT_REG_IDX_AR0 <= reg_idx <= R.XT_REG_IDX_ARLAST:
        idx = reg_idx - R.XT_REG_IDX_AR0
    elif R.XT_REG_IDX_A0 <= reg_idx <= R.XT_REG_IDX_A15:
        idx = reg_idx - R.XT_REG_IDX_A0
    else:
        raise ValueError(
            f"Can't convert register {reg_idx} to non-windowbased register")
    # Each windowbase value represents 4 registers on LX and 8 on NX
    base_inc = 4 if cache.core_type == XT_LX else 8
    return ((idx + windowbase * base_inc) & (cache.aregs_num - 1)) + R.XT_REG_IDX_AR0


def xtensa_canonical_to_windowbase_offset(cache, reg_idx, windowbase):
    """xtensa_windowbase_offset_to_canonical(xtensa, reg_idx, -windowbase)."""
    return xtensa_windowbase_offset_to_canonical(cache, reg_idx, -windowbase)


# --- DEBUGCAUSE / DSR.StopCause (xtensa_debug_module.h:297-334) ---------------
DEBUGCAUSE_IC = 1 << 0          # ICOUNT exception
DEBUGCAUSE_IB = 1 << 1          # IBREAK exception
DEBUGCAUSE_DB = 1 << 2          # DBREAK exception (watchpoint)
DEBUGCAUSE_BI = 1 << 3          # BREAK instruction encountered
DEBUGCAUSE_BN = 1 << 4          # BREAK.N instruction encountered
DEBUGCAUSE_DI = 1 << 5          # Debug Interrupt
DEBUGCAUSE_VALID = 1 << 31      # Pseudo-value to trigger reread (NX only)

OCDDSR_STOPCAUSE = 0xF << 5     # NX only
OCDDSR_STOPCAUSE_SHIFT = 5
OCDDSR_STOPCAUSE_DI = 0         # Debug Interrupt
OCDDSR_STOPCAUSE_SS = 1         # Single-step completed
OCDDSR_STOPCAUSE_IB = 2         # HW breakpoint (IBREAKn match)
OCDDSR_STOPCAUSE_B1 = 4         # SW breakpoint (BREAK.1 instruction)
OCDDSR_STOPCAUSE_BN = 5         # SW breakpoint (BREAK.N instruction)
OCDDSR_STOPCAUSE_B = 6          # SW breakpoint (BREAK instruction)
OCDDSR_STOPCAUSE_DB0 = 8        # HW watchpoint (DBREAK0 match)
OCDDSR_STOPCAUSE_DB1 = 9        # HW watchpoint (DBREAK0 match)


# --- xtensa.c:1161-1206 — xtensa_cause_get -----------------------------------
def xtensa_cause_get(cache, nx_state=None, read_dsr=None):
    """Read cause for entering halted state; return bitmask in DEBUGCAUSE_* format.

    LX (the S3): the cause lives in the cached DEBUGCAUSE register. NX path ported
    faithfully — it reads DSR.StopCause via `read_dsr()` and memoizes into
    `nx_state` (a 1-field object with .nx_stop_cause); not used on LX.
    """
    if cache.core_type == XT_LX:
        # LX cause in DEBUGCAUSE
        return xtensa_reg_get(cache, R.XT_REG_IDX_DEBUGCAUSE)

    if nx_state.nx_stop_cause & DEBUGCAUSE_VALID:
        return nx_state.nx_stop_cause

    # NX cause determined from DSR.StopCause
    dsr = read_dsr()
    cause = (dsr & OCDDSR_STOPCAUSE) >> OCDDSR_STOPCAUSE_SHIFT
    if cause == OCDDSR_STOPCAUSE_DI:
        nx_state.nx_stop_cause = DEBUGCAUSE_DI
    elif cause == OCDDSR_STOPCAUSE_SS:
        nx_state.nx_stop_cause = DEBUGCAUSE_IC
    elif cause == OCDDSR_STOPCAUSE_IB:
        nx_state.nx_stop_cause = DEBUGCAUSE_IB
    elif cause in (OCDDSR_STOPCAUSE_B, OCDDSR_STOPCAUSE_B1):
        nx_state.nx_stop_cause = DEBUGCAUSE_BI
    elif cause == OCDDSR_STOPCAUSE_BN:
        nx_state.nx_stop_cause = DEBUGCAUSE_BN
    elif cause in (OCDDSR_STOPCAUSE_DB0, OCDDSR_STOPCAUSE_DB1):
        nx_state.nx_stop_cause = DEBUGCAUSE_DB
    else:
        raise ValueError(f"Unknown stop cause (DSR: 0x{dsr:08x})")
    if nx_state.nx_stop_cause:
        nx_state.nx_stop_cause |= DEBUGCAUSE_VALID
    return nx_state.nx_stop_cause


# --- xtensa.c:1208-1218 — xtensa_cause_clear ---------------------------------
def xtensa_cause_clear(cache, nx_state=None):
    """LX: zero the cached DEBUGCAUSE and clear its dirty flag (it's a read-only
    cause, never written to the core). NX: clear the cached copy but keep VALID."""
    if cache.core_type == XT_LX:
        xtensa_reg_set(cache, R.XT_REG_IDX_DEBUGCAUSE, 0)
        cache.reg_list[R.XT_REG_IDX_DEBUGCAUSE].dirty = False
    else:
        # NX DSR.STOPCAUSE is not writeable; clear cached copy but leave it valid
        nx_state.nx_stop_cause = DEBUGCAUSE_VALID
