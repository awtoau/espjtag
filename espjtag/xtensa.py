"""espjtag.xtensa — ESP32-S3 Xtensa XDM debug access over the SAME esp_usb_jtag
transport as the RISC-V path (issue #9).

espjtag is RISC-V-native (RISC-V Debug Module). The Xtensa parts (S2/S3) use a
DIFFERENT debug module — the Xtensa On-Chip-Debug (XDM), reached via the Nexus
address/data register (NAR) — so this adds that protocol: NAR register read/write,
halt, and memory read/write via DIR0EXEC instruction injection. The S3 is then
reachable on the same transport (and tablable as a Tcl-bridge leaf command).

Everything is transcribed verbatim from openocd-esp32 src/target/xtensa/xtensa.c +
xtensa_debug_module.c, and validated on the bench register-for-register against
OpenOCD's own `drscan`/`irscan` and a live probe-rs memory read.

Two things make it work over espjtag's bit-banged transport:
  * the S3 is a TWO-TAP chain (chips.py tables it; the transport inserts the other
    TAP's BYPASS bit on every IR/DR scan);
  * a NAR access is ONE IR=NARSEL select then a single 8-bit addr scan ((nar<<1)|rw)
    + 32-bit data scan — and espjtag's transport lags the NAR read result by
    READ_LATENCY accesses vs OpenOCD's driver, flushed with priming reads.
"""

from . import chips

# TAP instructions + NAR register addresses (xtensa_debug_module.c/.h)
IR_PWRCTL, IR_PWRSTAT, IR_NARSEL = 0x08, 0x09, 0x1C
DCRSET, DCRCLR, DSR, DDR, DDREXEC, DIR0EXEC = 0x43, 0x42, 0x44, 0x45, 0x46, 0x47
OCDID = 0x40
OCDDCR_ENABLEOCD, OCDDCR_DEBUGINTERRUPT, OCDDSR_STOPPED = 0x01, 0x02, 0x10
# Xtensa instruction encodings, little-endian (DDR special reg 0x68, a3 = reg 3)
INS_RSR_DDR_A3 = 0x036830     # RSR  a3, DDR   (DDR special reg -> a3)
INS_WSR_DDR_A3 = 0x136830     # WSR  a3, DDR   (a3 -> DDR)
INS_LDDR32P_A3 = 0x0073E0     # LDDR32.P a3    (mem[a3] -> DDR, a3 += 4)
INS_SDDR32P_A3 = 0x0073F0     # SDDR32.P a3    (DDR -> mem[a3], a3 += 4)
# call_function — ported from OpenOCD xtensa_start_algorithm (xtensa.c:2810). The S3
# (LX7) debug level is 6: the debug-mode PC/PS live in EPC6 (sr 0xb6) / EPS6 (sr
# 0xc6). RFDO returns-from-debug (resumes execution at EPC6). a-regs set/read via DDR.
DEBUGLEVEL = 6
INS_WSR_EPC6_A3 = 0x13B630     # WSR a3, EPC6   (a3 -> debug PC)
INS_RSR_EPC6_A3 = 0x03B630     # RSR a3, EPC6
INS_WSR_EPS6_A3 = 0x13C630     # WSR a3, EPS6   (a3 -> debug PS)
INS_RSR_EPS6_A3 = 0x03C630     # RSR a3, EPS6
INS_RFDO = 0xF1E000            # return-from-debug-operation (resume at EPC6)
INS_BREAK = 0x004000           # BREAK 0,0 — the return trap (callee RETs into this)
# Windowed-ABI call support (#29). The ESP32 ROM SPI-flash helpers are WINDOWED
# (ENTRY/RETW) functions. Two approaches were tried and BENCH-MEASURED on the S3:
#   * Injecting `callx8` via DIR0EXEC does NOT commit the WindowBase increment (delta
#     measured 0) — DIR0EXEC-executed flow-control is neutralised. So injection can't
#     rotate the window.
#   * RFDO-resuming DIRECTLY onto a windowed `entry` instruction wedges the core (it
#     halts right after `entry`). A pure CALL0 callee resumed the same way runs fine.
# WORKING approach — a CALL0 BRIDGE STUB. We resume onto a tiny CALL0 stub (plain movs
# first) that reaches the windowed call through NATIVE execution, so `entry`/`retw`
# run as ordinary instructions (not the wedging RFDO-onto-entry case):
#   bridge: mov a10,a2 ... mov a15,a7 ; mov a9,a1 ; callx8 a8 ; mov a2,a10 ; ret.n
# Called via the proven _call0 path with a8 = the windowed ROM entry, args in a2..a7,
# a1 = SP; the stub forwards args to a10..a15 (-> callee a2..a7) and SP to a9 (->
# callee a1), does a NATIVE `callx8 a8` (real window rotate), then call0-returns the
# windowed result (callee a2 -> a10 -> a2). Encodings verified vs xtensa-esp32s3-elf-as.
# VALIDATED on the bench: a windowed leaf and esp_rom_spiflash_config_param (a leaf ROM
# fn) both return correctly through the bridge. LIMITATION: ROM functions that NEST
# several call8 levels (e.g. spi_flash_attach -> boot_attach -> SPI_init -> ...) still
# fault during window-overflow spill under resumed execution — see flash methods.
BRIDGE_STUB = [0x03BD02AD, 0x05DD04CD, 0x07FD06ED, 0x08E0019D, 0x0D0A2D00, 0x000000F0]
# WINDOWBASE (sr 0x48) / WINDOWSTART (sr 0x49) via a3 — wsr/rsr encodings from the
# assembler (verified). WSR WINDOWBASE clobbers a3; ROTW (below) rotates without a GPR.
INS_RSR_WB_A3 = 0x034830       # RSR a3, WINDOWBASE
INS_WSR_WB_A3 = 0x134830       # WSR a3, WINDOWBASE
INS_RSR_WS_A3 = 0x034930       # RSR a3, WINDOWSTART
INS_WSR_WS_A3 = 0x134930       # WSR a3, WINDOWSTART
# VECBASE (sr 0xe7) — the exception/interrupt vector-table base. We point it at the
# ROM's own table (0x40000000) for the call so a window overflow uses the ROM's
# _WindowOverflow{4,8,12} spill handlers. (On the bench app VECBASE is already
# 0x40000000, but we set it defensively, as OpenOCD's xtensa_start_algorithm does via
# its trap_entry_addr.) Encodings verified vs the assembler.
INS_RSR_VECBASE_A3 = 0x03E730  # RSR a3, VECBASE
INS_WSR_VECBASE_A3 = 0x13E730  # WSR a3, VECBASE
ROM_VECBASE = 0x40000000       # S3 ROM vector table base (window handlers @ +0x00..)
# ROTW imm — WindowBase += imm, WITHOUT using any GPR (0x408000|((imm&0xF)<<4), verified
# vs the assembler). Used for the all-AR save/restore around a windowed call. (A CALL8
# rotates WindowBase by +2 / 8 ARs, which is what the stub's native callx8 does.)
INS_ROTW_P1 = 0x408010         # rotw  1
NAREGS = 64                    # LX physical AR file size (16 windows of 4)
# PS (processor state) field we set in the run-PS (EPS6) for a windowed call.
PS_WOE = 0x40000               # PS.WOE bit18 — Window Overflow Enable (ENTRY/RETW active)
IDLE = 16                     # run-test-idle settle clocks (the XDM tdi_idle)
# READ_LATENCY = espjtag's NAR read-result pipeline depth over this transport: the
# captured data for a NAR read lags its address scan by this many *accesses*. read_mem
# over-reads nwords+READ_LATENCY and slices off the leading READ_LATENCY; nar_read
# primes READ_LATENCY+1 times and returns the last. Transport-specific and RE-DERIVED
# on HW (a known ramp written then read back, finding the slice that aligns —
# scripts/xcall_test.py). Was 2 in the bit-list-_recv era; the 2026-06-12 transport
# rework (aligned int _recv, capture-narrowing) removed the lag entirely -> 0. If a
# future transport change shifts the pipeline, re-derive (the ramp test) — do NOT
# guess; a wrong value silently shifts every read.
READ_LATENCY = 0

# ============================================================================
# Register cache model — PORTED FROM openocd-esp32 @ f10eceff22
#   src/target/xtensa/xtensa_regs.h  (enum xtensa_reg_id, enum xtensa_reg_type)
#   src/target/xtensa/xtensa.c:178-280  (struct xtensa_reg_desc xtensa_regs[])
# Built on the NAR transport above so xtensa_write_dirty_registers /
# xtensa_resume / xtensa_start_algorithm / xtensa_wait_algorithm can run 1:1
# against the C. Target is ESP32-S3 = LX7 (XT_LX); the NX branches are ported
# but never exercised.
# ============================================================================

# The register-descriptor model (enum xtensa_reg_id, enum xtensa_reg_type, the
# XT_PS/EPC/PC reg-num markers, and the verbatim 97-row xtensa_regs[] table) is
# the single source of truth in xtensa_regs.py — CI-drift-tested row-for-row vs
# openocd-esp32 @ f10eceff (scripts/test_xtensa_regs.py). Imported here so there
# is ONE table, not two. (Re-exported below for the modules that import from
# espjtag.xtensa.)
from .xtensa_regs import (                          # noqa: E402,F401
    XT_REG_IDX_PC, XT_REG_IDX_AR0, XT_REG_IDX_ARFIRST, XT_REG_IDX_AR3,
    XT_REG_IDX_AR15, XT_REG_IDX_ARLAST, XT_REG_IDX_WINDOWBASE,
    XT_REG_IDX_WINDOWSTART, XT_REG_IDX_PS, XT_REG_IDX_IBREAKENABLE,
    XT_REG_IDX_DDR, XT_REG_IDX_IBREAKA0, XT_REG_IDX_CPENABLE, XT_REG_IDX_EXCCAUSE,
    XT_REG_IDX_DEBUGCAUSE, XT_REG_IDX_ICOUNT, XT_REG_IDX_ICOUNTLEVEL,
    XT_REG_IDX_A0, XT_REG_IDX_A3, XT_REG_IDX_A15, XT_NUM_REGS,
    XT_REG_GENERAL, XT_REG_USER, XT_REG_SPECIAL, XT_REG_DEBUG, XT_REG_RELGEN,
    XT_REG_FR, XT_REG_TIE, XT_REG_OTHER, XT_REGF_NOREAD,
    XT_PS_REG_NUM, XT_EPC_REG_NUM_BASE, XT_PC_REG_NUM_VIRTUAL,
    xtensa_regs as _XTENSA_REGS_TABLE, XtensaRegDesc,
)

# xtensa.py-specific reg-num convenience constants not in the descriptor table.
XT_EPS_REG_NUM_BASE = 0xC0      # (EPS2 - 2), for adding DBGLEVEL
XT_VECBASE_REG_NUM = 0xE7
XT_SR_DDR = 0x68                # xtensa_regs[XT_REG_IDX_DDR].reg_num
XT_REG_A3 = 0x03                # xtensa_regs[XT_REG_IDX_AR3].reg_num

# PS.RING field (xtensa.h:39-41)
def XT_PS_RING(v):
    return (v & 0x3) << 6
XT_PS_RING_MSK = 0x3 << 6
def XT_PS_RING_GET(v):
    return (v >> 6) & 0x3

# enum xtensa_mode (xtensa.h:211-217)
XT_MODE_RING0, XT_MODE_RING1, XT_MODE_RING2, XT_MODE_RING3, XT_MODE_ANY = range(5)

# XT_INS_* little-endian encoders (LX is LE => XT_ISBE false; xtensa.c:44-138).
# _XT_INS_FORMAT_RSR (xtensa.c:44-50), LE branch:
def XT_INS_RSR(sr, t):          # Read Special Register (xtensa.c:131; opcode 0x030000)
    return 0x030000 | ((sr & 0xFF) << 8) | ((t & 0x0F) << 4)
def XT_INS_WSR(sr, t):          # Write Special Register (xtensa.c:133; opcode 0x130000)
    return 0x130000 | ((sr & 0xFF) << 8) | ((t & 0x0F) << 4)
def XT_INS_WUR(ur, t):          # Write User Register (xtensa.c:143; opcode 0xF30000)
    return 0xF30000 | ((ur & 0xFF) << 8) | ((t & 0x0F) << 4)
def XT_INS_WFR(fr, t):          # Write Floating-Point Register (xtensa.c:148, RRR LE)
    return 0xFA0000 | ((((t << 4) | 0x5) & 0xFF) << 4) | ((fr & 0x0F) << 12)
def XT_INS_ROTW(n):             # Rotate Window by N (xtensa.c:138, LE branch)
    return 0x408000 | ((n & 15) << 4)
XT_INS_RFDO = 0xF1E000          # "Return From Debug Operation" (xtensa.c:97, LE branch)

# debug.irq_level for the S3 (LX7) is 6: the debug-mode PC/PS live in EPC6/EPS6.
# (NDEBUGLEVEL — set per chip; here transcribed for the S3, the only LX we run.)
XT_LX_DEBUG_IRQ_LEVEL = 6
AREGS_NUM = 64                  # core_config->aregs_num for LX (16 windows of 4)

# OCDDSR exec-error bits (xtensa_debug_module.h:293-296) — checked by
# xtensa_core_status_check after each queue execute.
OCDDSR_EXECEXCEPTION = 1 << 1
OCDDSR_EXECBUSY = 1 << 2
OCDDSR_EXECOVERRUN = 1 << 3


# The verbatim 97-row xtensa_regs[] table now lives in xtensa_regs.py (imported
# above as _XTENSA_REGS_TABLE). Alias it under the historical name so existing
# references (and the re-export surface) keep working. Descriptors are
# XtensaRegDesc objects with attribute access (.name/.reg_num/.type/.dbreg_num).
xtensa_regs = _XTENSA_REGS_TABLE


def XT_MK_REG_DESC(name, reg_num, type_, flags):
    """Build one XtensaRegDesc (used for the S3's eps6 optreg, not in the base
    table). Same XtensaRegDesc as xtensa_regs.py so the cache is homogeneous."""
    d = XtensaRegDesc(name, reg_num, type_, flags)
    d.exist = True              # optregs we add are present on the S3
    return d


class _Reg:
    """struct reg (target/register.h) — only the fields xtensa.c touches. value
    is a uint32 (cache regs are <=32-bit here). dirty/valid/exist mirror the C."""
    __slots__ = ("name", "value", "dirty", "valid", "exist", "size")

    def __init__(self, name, exist=True, size=32):
        self.name = name
        self.value = 0
        self.dirty = False
        self.valid = False
        self.exist = exist
        self.size = size


class XtensaCore:
    """The register cache (struct reg_cache) + the xtensa_regs / optregs
    descriptor lookup + algo_context_backup. PORTED FROM xtensa.c. Owns:
      reg_list[]        — one _Reg per cache index
      regs[]            — descriptor for each cache index (xtensa_regs then optregs)
      optregs[]         — the optreg descriptors (we add just eps6 for the S3)
      eps_dbglevel_idx  — cache index of EPS[debug.irq_level] (xtensa.c:4093-4096)
    """

    def __init__(self):
        self.regs = list(xtensa_regs)                        # base XT_NUM_REGS descriptors
        self.optregs = []
        self.reg_list = [_Reg(d.name) for d in xtensa_regs]
        # ddr is XT_REGF_NOREAD; leave exist True (we never read it back). PC etc.
        # all exist on the S3.
        # --- optregs: the S3 has EPS6 (= EPS_BASE + irq_level) as the run-PS.
        # xtensa.c:4093-4096 sets eps_dbglevel_idx = XT_NUM_REGS + num_optregs - 1
        # for the optreg whose reg_num == XT_EPS_REG_NUM_BASE + debug.irq_level.
        eps_num = XT_EPS_REG_NUM_BASE + XT_LX_DEBUG_IRQ_LEVEL    # 0xC0 + 6 = 0xC6 (EPS6)
        self.optregs.append(XT_MK_REG_DESC("eps%d" % XT_LX_DEBUG_IRQ_LEVEL,
                                           eps_num, XT_REG_SPECIAL, 0))
        self.regs.append(self.optregs[-1])
        self.reg_list.append(_Reg(self.optregs[-1].name))
        self.eps_dbglevel_idx = XT_NUM_REGS + len(self.optregs) - 1
        self.num_regs = len(self.reg_list)
        # algo_context_backup[i] — per-reg saved value (xtensa.h). Sized to the cache.
        self.algo_context_backup = [0] * self.num_regs

    def desc(self, i):
        """The xtensa_reg_desc for cache index i (xtensa.c:726-727: base table for
        i < XT_NUM_REGS, else optregs[i - XT_NUM_REGS])."""
        return self.regs[i]

    def register_get_by_name(self, name):
        """register_get_by_name(core_cache, name, 0) (target/register.c:50) — first
        existing reg with this name, no search_all."""
        for r in self.reg_list:
            if not r.exist:
                continue
            if r.name == name:
                return r
        return None


class XtensaXDM:
    """Xtensa OCD access on an EspUsbJtag transport `j`. The S3 must be tabled
    two-TAP in chips.py so the transport carries the BYPASS bit per scan."""

    def __init__(self, j):
        self.j = j
        idcode = j.read_idcode()              # trigger the two-TAP chain detect
        # cache the chip dict from the fresh-open IDCODE: after XDM powerup/halt the
        # NAR traffic can leave read_idcode() returning stale values, so the flash
        # helpers must not re-derive the chip from a later read_idcode().
        self._chip_dict = chips.lookup(idcode) or {}
        # The register cache (struct xtensa.core_cache) the algorithm port runs on.
        self.core = XtensaCore()

    # --- NAR register access: ONE IR=NARSEL, single 8-bit addr + 32-bit data -----
    def nar_read(self, naraddr):
        """Read a side-effect-free XDM register (OCDID/DSR/DDR — NOT DDREXEC, which
        re-triggers DIR0 on each read). Reads READ_LATENCY+1 times to flush espjtag's
        NAR read latency and returns the last."""
        j = self.j
        j._drain_in()
        j._scan_ir(IR_NARSEL)
        val = 0
        for _ in range(READ_LATENCY + 1):
            j._scan_dr((naraddr << 1) | 0, 8)
            t = j._scan_dr(0, 32, capture=True)
            j._send()
            val = j._dr_field(j._recv(t), 0, 32)
        return val

    def nar_write(self, naraddr, value):
        j = self.j
        j._drain_in()
        j._scan_ir(IR_NARSEL)
        j._scan_dr((naraddr << 1) | 1, 8)
        j._scan_dr(value & 0xFFFFFFFF, 32)
        j._idle(IDLE)
        j._send()

    # --- power up the debug domain / halt / resume -------------------------------
    def powerup(self):
        """xtensa_examine: PWRCTL=0x07, 0x87 (| JTAGDEBUGUSE), then DCRSET=ENABLEOCD,
        as ONE batch (a TLR or split would lose the power state)."""
        j = self.j
        j._drain_in()
        j._scan_ir(IR_PWRCTL)
        j._scan_dr(0x07, 8); j._idle(IDLE)
        j._scan_dr(0x87, 8); j._idle(IDLE)
        j._scan_ir(IR_NARSEL)
        j._scan_dr((DCRSET << 1) | 1, 8)
        j._scan_dr(OCDDCR_ENABLEOCD, 32); j._idle(IDLE)
        j._send()

    def halt(self, tries=200, disable_wdt=True):
        self.nar_write(DCRSET, OCDDCR_ENABLEOCD | OCDDCR_DEBUGINTERRUPT)
        for _ in range(tries):
            if self.nar_read(DSR) & OCDDSR_STOPPED:
                if disable_wdt:
                    self._wdt_disable()
                return True
        return False

    def _wdt_disable(self):
        """Disable the S3 watchdogs so none resets the chip while halted — the
        same protection OpenOCD's esp32s3_on_halt applies on every halt (and the
        C5 #33 lesson). Data-driven from chips.py `wdt`; the super-WDT auto-feed
        is a read-modify-write. No-op on a chip without a wdt table. Core halted;
        all writes are uncached MMIO (now reliable, see write_mem fix)."""
        w = self._chip().get("wdt")
        if not w:
            return
        for wkey_reg, cfg_reg, cfg_val in w["disable"]:
            self.write_mem32(wkey_reg, w["key"])      # unlock
            self.write_mem32(cfg_reg, cfg_val)        # zero the config
        swd = w.get("swd")
        if swd:                                       # super-WDT: enable auto-feed
            self.write_mem32(swd["wprotect"], swd["key"])
            conf = self.read_mem32(swd["conf"]) | swd["auto_feed"]
            self.write_mem32(swd["conf"], conf)

    def resume(self):
        self.nar_write(DCRCLR, OCDDCR_DEBUGINTERRUPT)

    # --- call a function on the halted core (ported from xtensa_start_algorithm) --
    def _set_ar(self, n, val):
        """a<n> = val (RSR a<n>, DDR; doesn't touch a3 unless n==3)."""
        self.nar_write(DDR, val & 0xFFFFFFFF)
        self.nar_write(DIR0EXEC, 0x036800 | (n << 4))

    def _get_ar(self, n):
        """read a<n> (WSR a<n>, DDR; then read DDR)."""
        self.nar_write(DIR0EXEC, 0x136800 | (n << 4))
        return self.nar_read(DDR)

    def _set_sr_a3(self, wsr_ins, val):
        """special-reg <- val, using a3 as scratch (a3=val via DDR, then WSR a3,sr)."""
        self.nar_write(DDR, val & 0xFFFFFFFF)
        self.nar_write(DIR0EXEC, INS_RSR_DDR_A3)     # a3 = val
        self.nar_write(DIR0EXEC, wsr_ins)            # sr = a3

    def _get_sr_a3(self, rsr_ins):
        """read special-reg, using a3 as scratch (RSR a3,sr; WSR a3,DDR; read DDR)."""
        self.nar_write(DIR0EXEC, rsr_ins)            # a3 = sr
        self.nar_write(DIR0EXEC, INS_WSR_DDR_A3)     # DDR = a3
        return self.nar_read(DDR)

    # ======================================================================
    # Register cache access — PORTED FROM xtensa.c. xtensa_reg_get/set operate
    # on self.core.reg_list[idx]; the cache is flushed to silicon by
    # xtensa_write_dirty_registers. The cache is the model the algorithm port
    # below runs on, exactly like the C.
    # ======================================================================
    def _reg_get_value(self, reg):
        """xtensa_reg_get_value (xtensa.c:1031): buf_get_u32(reg->value, 0, 32)."""
        return reg.value & 0xFFFFFFFF

    def _reg_set_value(self, reg, value):
        """xtensa_reg_set_value (xtensa.c:1036): buf_set_u32 + reg->dirty = true."""
        reg.value = value & 0xFFFFFFFF
        reg.dirty = True

    def xtensa_reg_get(self, reg_id):
        """xtensa_reg_get (xtensa.c:1129)."""
        return self._reg_get_value(self.core.reg_list[reg_id])

    def xtensa_reg_set(self, reg_id, value):
        """xtensa_reg_set (xtensa.c:1136): no-op if unchanged, else set+dirty."""
        reg = self.core.reg_list[reg_id]
        if self._reg_get_value(reg) == (value & 0xFFFFFFFF):
            return
        self._reg_set_value(reg, value)

    def _wb_to_canonical(self, reg_idx, windowbase):
        """xtensa_windowbase_offset_to_canonical (xtensa.c:538). LX: base_inc 4."""
        if XT_REG_IDX_AR0 <= reg_idx <= XT_REG_IDX_ARLAST:
            idx = reg_idx - XT_REG_IDX_AR0
        elif XT_REG_IDX_A0 <= reg_idx <= XT_REG_IDX_A15:
            idx = reg_idx - XT_REG_IDX_A0
        else:
            raise ValueError("Can't convert register %d to non-windowbased" % reg_idx)
        base_inc = 4    # XT_LX
        return ((idx + windowbase * base_inc) & (AREGS_NUM - 1)) + XT_REG_IDX_AR0

    def xtensa_write_sr_by_num(self, sr_num, value):
        """xtensa_write_sr_by_num (xtensa.c:1021): DDR=value; a3=DDR; sr=a3."""
        self.nar_write(DDR, value)                                   # XDMREG_DDR
        self.nar_write(DIR0EXEC, XT_INS_RSR(XT_SR_DDR, XT_REG_A3))
        self.nar_write(DIR0EXEC, XT_INS_WSR(sr_num, XT_REG_A3))

    def xtensa_write_dirty_registers(self):
        """xtensa_write_dirty_registers (xtensa.c:705). Writes the dirty cache regs
        back to the core: SFR/USER/FR/TIE first (cpenable delayed), then A0..A15,
        then the AR file by rotating the window, then a3 restored. LX (windowed)
        path only; NX branches not ported (not exercised on the S3).

        Each `xtensa_queue_dbg_reg_write(DDR,v)` + `xtensa_queue_exec_ins(...)` pair
        maps to nar_write(DDR,v) / nar_write(DIR0EXEC, ins); the C batches them into
        one queue execute, our nar_write serialises each (IDLE-clocked)."""
        core = self.core
        reg_list = core.reg_list
        reg_list_size = core.num_regs
        scratch_reg_dirty = False
        delay_cpenable = False
        windowbase = 0
        a3 = 0

        # SFR/USER/FR/TIE first (xtensa.c:725-790)
        for i in range(reg_list_size):
            rlist = (xtensa_regs if i < XT_NUM_REGS else core.optregs)
            ridx = i if i < XT_NUM_REGS else i - XT_NUM_REGS
            if reg_list[i].dirty:
                rd = rlist[ridx]
                if rd.type in (XT_REG_SPECIAL, XT_REG_USER, XT_REG_FR, XT_REG_TIE):
                    scratch_reg_dirty = True
                    if i == XT_REG_IDX_CPENABLE:
                        delay_cpenable = True
                        continue
                    regval = self.xtensa_reg_get(i)
                    self.nar_write(DDR, regval)
                    self.nar_write(DIR0EXEC, XT_INS_RSR(XT_SR_DDR, XT_REG_A3))
                    if reg_list[i].exist:
                        reg_num = rd.reg_num
                        if rd.type == XT_REG_USER:
                            self.nar_write(DIR0EXEC, XT_INS_WUR(reg_num, XT_REG_A3))
                        elif rd.type == XT_REG_FR:
                            self.nar_write(DIR0EXEC, XT_INS_WFR(reg_num, XT_REG_A3))
                        elif rd.type == XT_REG_TIE:
                            pass    # tie_reg_access — not configured on the S3
                        else:       # SFR
                            if reg_num == XT_PC_REG_NUM_VIRTUAL:
                                # PC for the debug interrupt depends on NDEBUGLEVEL
                                reg_num = XT_EPC_REG_NUM_BASE + XT_LX_DEBUG_IRQ_LEVEL
                                self.nar_write(DIR0EXEC, XT_INS_WSR(reg_num, XT_REG_A3))
                            else:
                                self.nar_write(DIR0EXEC, XT_INS_WSR(reg_num, XT_REG_A3))
                    reg_list[i].dirty = False
        # NOTE: the MS (NX) delay path (xtensa.c:774-780/913-919) is NX-only; omitted.
        if scratch_reg_dirty:
            reg_list[XT_REG_IDX_A3].dirty = True            # xtensa_mark_register_dirty(A3)
        if delay_cpenable:                                  # xtensa.c:793-802
            regval = self.xtensa_reg_get(XT_REG_IDX_CPENABLE)
            self.nar_write(DDR, regval)
            self.nar_write(DIR0EXEC, XT_INS_RSR(XT_SR_DDR, XT_REG_A3))
            self.nar_write(DIR0EXEC,
                           XT_INS_WSR(xtensa_regs[XT_REG_IDX_CPENABLE].reg_num, XT_REG_A3))
            reg_list[XT_REG_IDX_CPENABLE].dirty = False

        preserve_a3 = True      # windowed (LX) — xtensa.c:804
        if preserve_a3:         # save (windowed) A3 for scratch use (xtensa.c:805-814)
            self.nar_write(DIR0EXEC, XT_INS_WSR(XT_SR_DDR, XT_REG_A3))   # DDR = a3
            a3 = self.nar_read(DDR)

        # windowed: grab WINDOWBASE; the ARx/Ax mismatch warning (xtensa.c:824-851)
        # is diagnostic only and omitted.
        windowbase = self.xtensa_reg_get(XT_REG_IDX_WINDOWBASE)

        # Write A0-A15 (xtensa.c:854-870)
        for i in range(16):
            if reg_list[XT_REG_IDX_A0 + i].dirty:
                regval = self.xtensa_reg_get(XT_REG_IDX_A0 + i)
                self.nar_write(DDR, regval)
                self.nar_write(DIR0EXEC, XT_INS_RSR(XT_SR_DDR, i))
                reg_list[XT_REG_IDX_A0 + i].dirty = False
                if i == 3:
                    a3 = regval     # avoid stomping A3 during restore at end

        # Write AR registers, rotating the window (xtensa.c:872-911)
        for j in range(0, XT_REG_IDX_ARLAST, 16):
            for i in range(16):
                if i + j < AREGS_NUM:
                    realadr = self._wb_to_canonical(XT_REG_IDX_AR0 + i + j, windowbase)
                    if reg_list[realadr].dirty:
                        regval = self.xtensa_reg_get(realadr)
                        self.nar_write(DDR, regval)
                        self.nar_write(DIR0EXEC,
                                       XT_INS_RSR(XT_SR_DDR,
                                                  xtensa_regs[XT_REG_IDX_AR0 + i].reg_num))
                        reg_list[realadr].dirty = False
                        if (i + j) == 3:
                            a3 = regval
            # rotate window so we see the next 16; final rotate wraps to start.
            self.nar_write(DIR0EXEC, XT_INS_ROTW(4))         # LX: 4

        if preserve_a3:                                      # restore a3 (xtensa.c:921-924)
            self.nar_write(DDR, a3)
            self.nar_write(DIR0EXEC, XT_INS_RSR(XT_SR_DDR, XT_REG_A3))

    # ======================================================================
    # Resume — PORTED FROM xtensa.c. xtensa_resume -> prepare_resume (writes PC,
    # hw breakpoints, flush dirty regs) -> do_resume (RFDO). The S3 has no hw
    # breakpoints set in this path; the watchpoint/break single-step (which needs
    # DEBUGCAUSE) is skipped because we always resume with address && !current.
    # ======================================================================
    def xtensa_prepare_resume(self, current, address, handle_breakpoints,
                              debug_execution):
        """xtensa_prepare_resume (xtensa.c:1648)."""
        bpena = 0
        # halt_request = false (xtensa.c:1668) — modelled by not re-asserting it.
        if address and not current:
            self.xtensa_reg_set(XT_REG_IDX_PC, address)     # xtensa.c:1670-1671
        # else: DEBUGCAUSE-driven single-step (xtensa.c:1672-1688) — not reached
        # here (start_algorithm always resumes address && !current).
        # Write back hw breakpoints (xtensa.c:1692-1700) — none set: bpena stays 0.
        self.xtensa_reg_set(XT_REG_IDX_IBREAKENABLE, bpena)  # LX (xtensa.c:1701-1702)
        self.xtensa_write_dirty_registers()                  # xtensa.c:1704-1705

    def xtensa_do_resume(self):
        """xtensa_do_resume (xtensa.c:1711-1726): cause_reset (NX no-op on LX),
        exec RFDO, then core status check. VERBATIM: the C body's only wire op on
        LX is RFDO — it does NOT clear DEBUGINTERRUPT first (RFDO returns-from-debug
        by itself). The earlier DCRCLR-before-RFDO was a paraphrase that wedged the
        core on nested-call8 debug re-entry (the #29 flash_map_get hang)."""
        # xtensa_cause_reset is NX-only (clears nx_stop_cause); no-op for LX.
        self.nar_write(DIR0EXEC, XT_INS_RFDO)            # xtensa.c:1718 (RFDO only)
        # xtensa_core_status_check omitted (diagnostic; reads DSR exec-error bits).

    def xtensa_resume(self, current, address, handle_breakpoints, debug_execution):
        """xtensa_resume (xtensa.c:1728)."""
        self.xtensa_prepare_resume(current, address, handle_breakpoints,
                                   debug_execution)
        self.xtensa_do_resume()
        # target->debug_reason / state transitions (xtensa.c:1747-1753) are host-side
        # bookkeeping we don't model.

    # ======================================================================
    # xtensa_start_algorithm / xtensa_wait_algorithm — PORTED FROM xtensa.c:2810-3017.
    # Rebuilt on the register cache + xtensa_resume. reg_params is a list of
    # (reg_name, value, direction) with direction "out" (== PARAM_OUT, write only)
    # or "inout" (== PARAM_IN_OUT, write before + read back). mem_params and the
    # stub exit BREAK are managed by the caller (the flasher); trap_entry_addr maps
    # to algorithm_info->trap_entry_addr (sets VECBASE), core_mode to ->core_mode.
    # NOTE the C reaches registers via register_get_by_name on the cache — 'vecbase'
    # is NOT a cache register, so it must come via trap_entry_addr, not a reg_param.
    # ======================================================================
    def start_algorithm(self, reg_params, entry_point, exit_point=0,
                        trap_entry_addr=0, core_mode=XT_MODE_ANY, algorithm_info=None):
        """xtensa_start_algorithm (xtensa.c:2810). algorithm_info is the
        xtensa_algorithm dict (ctx_debug_reason/ctx_ps saved into it for wait);
        created here if not supplied. Returns the algorithm_info so wait can restore."""
        core = self.core
        usr_ps = False
        if algorithm_info is None:
            algorithm_info = {"core_mode": core_mode, "trap_entry_addr": trap_entry_addr}

        # NOTE: caller asserts target->state == TARGET_HALTED (xtensa.c:2825).

        # backup whole core_cache (xtensa.c:2830-2833)
        for i in range(core.num_regs):
            core.algo_context_backup[i] = self.xtensa_reg_get(i)
        # save debug reason — not modelled host-side. (xtensa.c:2835/2839)
        algorithm_info["ctx_debug_reason"] = None
        # save PS and set to debug_level - 1 (XT_LX) (xtensa.c:2840-2845)
        algorithm_info["ctx_ps"] = self.xtensa_reg_get(core.eps_dbglevel_idx)
        newps = (algorithm_info["ctx_ps"] & ~0xF) | (XT_LX_DEBUG_IRQ_LEVEL - 1)
        self.xtensa_reg_set(core.eps_dbglevel_idx, newps)

        # write mem params (xtensa.c:2846-2855) — caller's job (flasher writes
        # mem-arg buffers before calling). No-op here.

        # write reg params (xtensa.c:2856-2880)
        for name, value, direction in reg_params:
            reg = core.register_get_by_name(name)
            if reg is None:
                raise RuntimeError("BUG: register '%s' not found" % name)
            # size check (xtensa.c:2867-2870) — all cache regs are 32-bit.
            # memcmp(reg_name, "ps", 3) compares 3 bytes incl. the NUL => true (set
            # usr_ps) iff name is NOT exactly "ps".
            if name != "ps":
                usr_ps = True
            else:                       # XT_LX: ps maps to eps_dbglevel_idx
                reg = core.reg_list[core.eps_dbglevel_idx]
            self._reg_set_value(reg, value & 0xFFFFFFFF)
            reg.valid = True

        # Set stub exception vector table (xtensa.c:2882-2889)
        if algorithm_info["trap_entry_addr"]:
            self.xtensa_write_sr_by_num(XT_VECBASE_REG_NUM,
                                        algorithm_info["trap_entry_addr"] & 0xFFFFFFFF)

        # ignore custom core mode if custom PS value is specified (xtensa.c:2891-2905)
        if not usr_ps:      # XT_LX
            eps_reg_idx = core.eps_dbglevel_idx
            ps = self.xtensa_reg_get(eps_reg_idx)
            cur_core_mode = XT_PS_RING_GET(ps)
            if (algorithm_info["core_mode"] != XT_MODE_ANY
                    and algorithm_info["core_mode"] != cur_core_mode):
                new_ps = (ps & ~XT_PS_RING_MSK) | XT_PS_RING(algorithm_info["core_mode"])
                # core_mode not restored for now (xtensa.c:2900-2901)
                algorithm_info["core_mode"] = cur_core_mode
                self.xtensa_reg_set(eps_reg_idx, new_ps)
                core.reg_list[eps_reg_idx].valid = True

        # xtensa_resume(current=false, entry_point, handle_breakpoints=true,
        # debug_execution=true) (xtensa.c:2907)
        self.xtensa_resume(False, entry_point, True, True)
        return algorithm_info

    def wait_algorithm(self, reg_params, exit_point=0, timeout=4000,
                       algorithm_info=None):
        """xtensa_wait_algorithm (xtensa.c:2911). Polls DSR until STOPPED (the exit
        BREAK), force-halts on failure, checks pc == exit_point, copies inout
        reg_params out, then restores the whole core_cache from the backup and
        flushes. Returns (out_dict, halted). out_dict maps each inout reg name to
        its post-run value (buf_set_u32 of xtensa_reg_get_value)."""
        core = self.core

        # target_wait_state(HALTED, timeout) (xtensa.c:2925)
        halted = False
        for _ in range(timeout):
            if self.nar_read(DSR) & OCDDSR_STOPPED:
                halted = True
                break
        if not halted:
            # force a halt (xtensa.c:2927-2938)
            self.halt()
            halted2 = False
            for _ in range(500):                # 500 ms (xtensa.c:2931)
                if self.nar_read(DSR) & OCDDSR_STOPPED:
                    halted2 = True
                    break
            if not halted2:
                return {}, False                # ERROR_TARGET_TIMEOUT

        # Read PC from the core, into the cache, then check it (xtensa.c:2940-2944).
        # PC lives in EPC[debug.irq_level]; the cache fetch route is xtensa_reg_get
        # after a fetch_all — here we read EPC6 directly off the core and seed PC.
        pc = self._get_sr_a3(INS_RSR_EPC6_A3)
        self._reg_set_value(core.reg_list[XT_REG_IDX_PC], pc)
        core.reg_list[XT_REG_IDX_PC].dirty = False
        if exit_point and pc != exit_point:
            raise RuntimeError(
                "failed algorithm halted at 0x%08x, expected 0x%08x" % (pc, exit_point))

        # Copy core register values to reg_params[] (xtensa.c:2945-2959)
        out = {}
        for name, _value, direction in reg_params:
            if direction != "out":      # direction != PARAM_OUT
                reg = core.register_get_by_name(name)
                if reg is None:
                    raise RuntimeError("BUG: register '%s' not found" % name)
                # The inout reg's live value: read it off the core, seed the cache.
                if name[0] == "a" and name[1:].isdigit():
                    live = self._get_ar(int(name[1:]))
                    self._reg_set_value(reg, live)
                    reg.dirty = False
                out[name] = self._reg_get_value(reg)

        # Read memory values to mem_params (xtensa.c:2960-2970) — caller's job.

        # Restore core_cache from the backup, high index to low (xtensa.c:2975-3007).
        for i in range(core.num_regs - 1, -1, -1):
            reg = core.reg_list[i]
            if i == XT_REG_IDX_PS:
                continue        # restore mapped reg number of PS depends on NDEBUGLEVEL
            elif i == XT_REG_IDX_DEBUGCAUSE:
                # restoring DEBUGCAUSE causes exception in DIR — copy back, clear flags.
                reg.value = core.algo_context_backup[i] & 0xFFFFFFFF
                reg.dirty = False
                reg.valid = False
            elif (core.algo_context_backup[i] & 0xFFFFFFFF) != (reg.value & 0xFFFFFFFF):
                reg.value = core.algo_context_backup[i] & 0xFFFFFFFF
                reg.dirty = True
                reg.valid = True
        # target->debug_reason = ctx_debug_reason — host-side, not modelled.
        # XT_LX: restore EPS[dbglevel] from ctx_ps (xtensa.c:3009-3010)
        if algorithm_info is not None:
            self.xtensa_reg_set(core.eps_dbglevel_idx, algorithm_info["ctx_ps"])

        self.xtensa_write_dirty_registers()     # xtensa.c:3012
        return out, halted or True

    def call_function(self, entry, args=(), stack=None, trap=None, timeout=4000,
                      windowed=True, bridge=None):
        """Call on-target code at `entry` with up to 6 int args; returns
        (retval, halted). Core MUST be halted.

        windowed=True (default — the ESP ROM SPI-flash helpers are ENTRY/RETW
        functions): call via a CALL0 BRIDGE STUB so the windowed `entry`/`retw` run as
        native execution (see _call_windowed / the BRIDGE_STUB note). `bridge` is an
        IRAM address to stage the 24-byte stub (defaults to `stack`-0x400, clear of the
        downward SP). Validated for leaf ROM functions (config_param); ROM functions
        that nest several call8 levels are not yet supported (window-spill — see the
        flash methods / #29). windowed=False: the OpenOCD xtensa_start_algorithm call0
        form (a0=BREAK-trap, a1=sp, a2..=args) — for a CALL0 entry / a bare BREAK."""
        if windowed:
            if bridge is None:
                bridge = (stack - 0x400) if stack is not None else (trap - 0x400)
            return self._call_windowed(entry, args, stack, trap, timeout, bridge)
        return self._call0(entry, args, stack, trap, timeout)

    def _call0(self, entry, args, stack, trap, timeout):
        """CALL0-ABI call. Verbatim port of OpenOCD xtensa_start_algorithm/
        wait_algorithm (xtensa.c:2810): save the clobbered a0..a7 + EPC6/EPS6; set
        run PS = (debug PS & ~0xf) | (DEBUGLEVEL-1); set EPC6=entry; load a0=BREAK-
        trap, a1=sp, a2..=args; RFDO to resume; poll DSR STOPPED; read a2; restore."""
        saved_ar = {n: self._get_ar(n) for n in range(8)}
        saved_epc = self._get_sr_a3(INS_RSR_EPC6_A3)
        saved_eps = self._get_sr_a3(INS_RSR_EPS6_A3)
        self.write_mem(trap, [INS_BREAK])                       # return trap
        # special regs first (they use a3 as scratch), then the arg regs LAST so a3
        # (which may hold an arg) isn't clobbered before the resume.
        self._set_sr_a3(INS_WSR_EPS6_A3, (saved_eps & ~0xF) | (DEBUGLEVEL - 1))
        self._set_sr_a3(INS_WSR_EPC6_A3, entry)                 # PC = entry
        self._set_ar(0, trap)                                   # a0 = return trap
        if stack is not None:
            self._set_ar(1, stack)                              # a1 = sp
        for i, a in enumerate(args):
            self._set_ar(2 + i, a)                              # a2.. = args
        self.nar_write(DIR0EXEC, INS_RFDO)                      # resume at EPC6
        halted = False
        for _ in range(timeout):
            if self.nar_read(DSR) & OCDDSR_STOPPED:
                halted = True
                break
        ret = self._get_ar(2) if halted else None               # a2 = return value
        self._set_sr_a3(INS_WSR_EPC6_A3, saved_epc)             # restore
        self._set_sr_a3(INS_WSR_EPS6_A3, saved_eps)
        for n, v in saved_ar.items():
            self._set_ar(n, v)
        return ret, halted

    def _save_all_ars(self):
        """Save all 64 physical ARs, leaving WindowBase unchanged. Steps the window
        with ROTW 1 sixteen times (net +16 ≡ identity), reading a0..a3 each step
        (non-destructive: WSR a<n>,DDR copies a<n>, doesn't clobber it). Returns a
        list[64] indexed by offset from the CURRENT window: out[w*4+n] = a<n> at
        WindowBase+w. _restore_all_ars must run from the same starting WindowBase."""
        out = []
        for _ in range(NAREGS // 4):
            out += [self._get_ar(n) for n in range(4)]
            self.nar_write(DIR0EXEC, INS_ROTW_P1)              # WindowBase += 1
        return out

    def _restore_all_ars(self, saved):
        """Inverse of _save_all_ars — write all 64 ARs, net WindowBase unchanged.
        Must be called with WindowBase at the SAME value _save_all_ars started from."""
        for w in range(NAREGS // 4):
            for n in range(4):
                self._set_ar(n, saved[w * 4 + n])
            self.nar_write(DIR0EXEC, INS_ROTW_P1)              # WindowBase += 1

    def _call_windowed(self, entry, args, stack, trap, timeout, bridge):
        """WINDOWED-ABI call via the CALL0 BRIDGE STUB (see the BRIDGE_STUB note above).
        Up to 6 args (callee a2..a7). Returns (return-value, halted). Core HALTED.

        We write the 24-byte stub to `bridge` (IRAM), then resume CALL0-style onto it
        (a0=trap return, a1=SP, a2..a7=args, a8=`entry` = the windowed ROM target). The
        stub forwards args->a10..a15 and SP->a9, does a NATIVE `callx8 a8` (real window
        rotate — the ROM's ENTRY/RETW then run as ordinary execution), and call0-returns
        the windowed result (callee a2 -> a10 -> a2) to the BREAK trap.

        Window state for the call: WINDOWSTART = 1<<caller_wb (only the stub's frame
        live, so the callee's ENTRY doesn't collide with a stale app window), VECBASE =
        ROM table (its window-overflow handlers), run-PS = app PS with INTLEVEL=
        DEBUGLEVEL-1 and WOE=1. All 64 ARs + EPC6/EPS6/WINDOWSTART/VECBASE are saved and
        restored so a merely-halted app resumes intact."""
        saved_epc = self._get_sr_a3(INS_RSR_EPC6_A3)
        saved_eps = self._get_sr_a3(INS_RSR_EPS6_A3)
        saved_wb = self._get_sr_a3(INS_RSR_WB_A3)
        saved_ws = self._get_sr_a3(INS_RSR_WS_A3)
        saved_vb = self._get_sr_a3(INS_RSR_VECBASE_A3)
        saved_ars = self._save_all_ars()                       # leaves WindowBase = saved_wb
        self.write_mem(bridge, BRIDGE_STUB)                    # stage the CALL0 bridge
        self.write_mem(trap, [INS_BREAK])                      # return trap
        # a valid parent-SP link below the SP so a window overflow of the stub frame
        # can spill (the ABI base-save area at [sp-12] = parent SP).
        if stack is not None:
            self.write_mem(stack - 16, [0, stack + 0x100, 0, 0])
        run_ps = (saved_eps & ~0xF) | (DEBUGLEVEL - 1) | PS_WOE
        self._set_sr_a3(INS_WSR_EPS6_A3, run_ps)
        self._set_sr_a3(INS_WSR_EPC6_A3, bridge)               # resume onto the stub
        self._set_sr_a3(INS_WSR_VECBASE_A3, ROM_VECBASE)       # ROM window-spill handlers
        self._set_sr_a3(INS_WSR_WS_A3, 1 << (saved_wb & 0xF))  # only the stub frame live
        self._set_ar(0, trap & 0xFFFFFFFF)                     # call0 return (ret.n -> a0)
        if stack is not None:
            self._set_ar(1, stack)                             # SP
        self._set_ar(8, entry)                                 # callx8 target = ROM entry
        for i, a in enumerate(args):
            self._set_ar(2 + i, a)                             # call0 args a2..a7
        # resume onto the stub. RFDO only (xtensa.c:1718 xtensa_do_resume) — do NOT
        # clear DEBUGINTERRUPT first; RFDO returns-from-debug by itself, and the extra
        # DCRCLR wedged the core on nested-call8 debug re-entry (#29).
        self.nar_write(DIR0EXEC, INS_RFDO)
        halted = False
        for _ in range(timeout):
            if self.nar_read(DSR) & OCDDSR_STOPPED:
                halted = True
                break
        ret = self._get_ar(2) if halted else None              # call0 return value in a2
        self._set_sr_a3(INS_WSR_WB_A3, saved_wb)               # defensive: restore window
        self._set_sr_a3(INS_WSR_WS_A3, saved_ws)
        self._set_sr_a3(INS_WSR_VECBASE_A3, saved_vb)
        self._set_sr_a3(INS_WSR_EPC6_A3, saved_epc)
        self._set_sr_a3(INS_WSR_EPS6_A3, saved_eps)
        self._restore_all_ars(saved_ars)
        return ret, halted

    # --- a3 save/restore around instruction injection (core must be HALTED) -------
    # read_mem/write_mem stage the target address in a3 and stream LDDR32.P/SDDR32.P
    # off it, which CLOBBERS a3. If the app was merely halted (not reset) we must
    # hand a3 back unchanged on resume, so each access brackets itself with these.
    # Both are built only from the already-validated nar_read/nar_write primitives
    # (each does its own IR=NARSEL select), so they are correct by reuse and need no
    # new latency bookkeeping — nar_read primes the READ_LATENCY pipeline itself.
    def _save_a3(self):
        """Return the live a3. WSR a3,DDR copies a3 into DDR (the inverse of the
        RSR DDR,a3 the streaming uses); then read DDR back, latency-primed."""
        self.nar_write(DIR0EXEC, INS_WSR_DDR_A3)   # DDR = a3   (exec WSR a3, DDR)
        return self.nar_read(DDR)                   # a3 value, READ_LATENCY-primed

    def _restore_a3(self, saved):
        """Put `saved` back into a3. DDR = saved, then RSR DDR,a3 moves it to a3 —
        the same two-step staging read_mem/write_mem use for the address."""
        self.nar_write(DDR, saved)                  # DDR = saved a3
        self.nar_write(DIR0EXEC, INS_RSR_DDR_A3)    # a3 = DDR = saved (exec RSR)

    # --- memory via DIR0EXEC instruction injection (core must be HALTED) ----------
    def read_mem(self, addr, nwords):
        """Read nwords 32-bit words from `addr`. Stage addr in a3 (RSR DDR,a3), exec
        LDDR32.P a3 once (DDR=mem[a3], a3+=4), then stream: read DDREXEC (returns the
        word AND re-triggers the load) for all but the last, plain DDR for the last.
        Saves+restores a3 so a halted app resumes intact. (One IR=NARSEL; single
        addr+data per access; latency primed.)"""
        j = self.j
        saved_a3 = self._save_a3()                # preserve the app's a3
        j._drain_in()
        j._scan_ir(IR_NARSEL); j._send()

        def acc(naraddr, rw, data=0, cap=False):
            j._scan_dr((naraddr << 1) | rw, 8); j._send()
            t = j._scan_dr(data & 0xFFFFFFFF, 32, capture=cap); j._send()
            return j._dr_field(j._recv(t), 0, 32) if cap else None

        acc(DDR, 1, addr)                         # DDR = addr
        acc(DIR0EXEC, 1, INS_RSR_DDR_A3)          # a3 = DDR = addr
        acc(DIR0EXEC, 1, INS_LDDR32P_A3)          # DDR = mem[a3]; a3 += 4 (word0)
        total = nwords + READ_LATENCY
        out = [acc(DDR if k == total - 1 else DDREXEC, 0, 0, cap=True)
               for k in range(total)]
        self._restore_a3(saved_a3)                # hand a3 back to the app
        return out[READ_LATENCY:READ_LATENCY + nwords]

    def write_mem(self, addr, words):
        """Write `words` from `addr`. 1:1 with OpenOCD xtensa_write_memory
        (probe_lsddr32p path): stage addr in a3, load the SDDR32.P instruction
        into DIR0 ONCE for word0, then STREAM the rest through DDREXEC — writing
        DDREXEC sets DDR AND re-executes the DIR0 instruction (mem[a3]=DDR;
        a3+=4). The old code re-injected the instruction via DIR0EXEC every word,
        which didn't land on silicon (#29). Saves+restores a3.

        Each access carries IDLE run-test clocks so the injected instruction
        completes before the next NAR access — matching OpenOCD's queue-execute
        serialization."""
        j = self.j
        words = list(words)
        if not words:
            return 0
        saved_a3 = self._save_a3()                # preserve the app's a3
        j._drain_in()
        j._scan_ir(IR_NARSEL); j._send()

        def acc(naraddr, data):
            j._scan_dr((naraddr << 1) | 1, 8)
            j._scan_dr(data & 0xFFFFFFFF, 32)
            j._idle(IDLE)                         # let an injected instr commit
            j._send()

        acc(DDR, addr)                            # DDR = addr
        acc(DIR0EXEC, INS_RSR_DDR_A3)             # a3 = DDR = addr
        acc(DDR, words[0])                        # DDR = word0
        acc(DIR0EXEC, INS_SDDR32P_A3)             # mem[a3]=DDR; a3+=4  (load+exec)
        for w in words[1:]:
            acc(DDREXEC, w)                       # DDR=w AND re-exec SDDR32.P
        self._restore_a3(saved_a3)                # hand a3 back to the app
        return len(words)

    def read_mem32(self, addr):
        return self.read_mem(addr, 1)[0]

    def write_mem32(self, addr, value):
        self.write_mem(addr, [value])

    # === flash over JTAG — call the ROM esp_rom_spiflash_* (#29, mirrors debug.py) ===
    # Same Option-A approach as the RISC-V path (debug.EspUsbJtag.flash_*): build on
    # call_function + the per-chip ROM/SRAM table, GATED behind a 0xE9 read-back self-
    # test so we never erase/program a misconfigured chip. The Xtensa difference is the
    # WINDOWED ABI — call_function(windowed=True) handles it via the CALL0 bridge.
    #
    # *** STATUS on the S3 (#29), corrected 2026-06-12 after the read-latency fix:
    # The old note here ("legacy_data/funcs are NULL; needs spi_flash_attach") was a
    # READ-BUG ARTIFACT — the stale READ_LATENCY=2 made every memory read return
    # shifted garbage, so the legacy pointers LOOKED null. With reads fixed, the
    # running awto-esp-base app has the legacy ROM flash state FULLY INTACT:
    #   legacy_data  (0x3FCEFFE4) -> 0x3fcef6a4 = {devid 0x852018, size 16MB, 64K
    #                blk, 4K sect, 256 page, 0xffff mask} — geometry already set
    #   legacy_funcs (0x3FCEFFE8) -> 0x3fcef670 = the funcs table
    # So NO flash_init / spi_flash_attach is needed on this app. The remaining
    # blocker is NOT the legacy state — it's the resume-and-trap EXECUTION path:
    # after RFDO the core does not re-halt at the BREAK trap (call_function returns
    # halted=False). That needs an OpenOCD-verbatim port of xtensa_start_algorithm/
    # wait_algorithm (debug-exception-on-return setup), which is the next #29 step.
    # Until then _rom_flash_ready() returns False and flash_write() refuses. ***
    LEGACY_DATA_PTR = 0x3FCEFFE4    # &rom_spiflash_legacy_data (esp32s3.rom.ld)
    LEGACY_FUNCS_PTR = 0x3FCEFFE8   # &rom_spiflash_legacy_funcs

    def _chip(self):
        return self._chip_dict

    def call_rom(self, sym, args=()):
        """Call a named ROM function from this chip's chips.rom table (windowed). The
        core must be halted and a scratch SRAM window must be tabled. Returns
        (a2/return-value, halted)."""
        c = self._chip()
        rom, sram = c.get("rom"), c.get("sram")
        if not rom or sym not in rom:
            raise RuntimeError(f"call_rom: no ROM symbol {sym!r} tabled for "
                               f"{c.get('name', '?')}")
        if not sram:
            raise RuntimeError("call_rom: no scratch SRAM window tabled")
        return self.call_function(rom[sym], args=args, stack=sram["stack"],
                                  trap=sram["trap"])

    def flash_init(self, chip_size=0x1000000):
        """Repopulate the legacy ROM spiflash geometry so esp_rom_spiflash_* helpers
        work. Points rom_spiflash_legacy_data at the scratch chip-struct buffer (it is
        NULL on a running app), then esp_rom_spiflash_config_param(devid=0, chip_size,
        64 KiB block, 4 KiB sector, 256 B page, 0xFFFF mask) populates it. (spi_flash_
        attach — which would set up dummy-cycle fields + rom_spiflash_legacy_funcs — is
        NOT run yet: it nests too deep for the current windowed-call path, see #29.)
        Hart must be halted. Returns the ROM result (0 = OK for the config_param step)."""
        c = self._chip()
        rom, sram = c.get("rom"), c.get("sram")
        if not rom or "spiflash_config_param" not in rom:
            raise RuntimeError("flash_init: spiflash_config_param not tabled for this chip")
        chip_buf = sram["data"] + 0x1400        # scratch g_rom_spiflash_chip struct
        self.write_mem(chip_buf, [0] * 8)
        if self.read_mem32(self.LEGACY_DATA_PTR) == 0:
            self.write_mem32(self.LEGACY_DATA_PTR, chip_buf)
        r, _ = self.call_rom("spiflash_config_param",
                             args=(0, chip_size, 0x10000, 0x1000, 0x100, 0xFFFF))
        return r

    def flash_read_rom(self, addr, nwords):
        """Read nwords from RAW flash byte-offset `addr` via esp_rom_spiflash_read
        (needs flash_init / a configured legacy layer). Hart halted. Reuses scratch."""
        c = self._chip()
        buf = c["sram"]["data"]
        self.call_rom("spiflash_read", args=(addr, buf, nwords * 4))
        return self.read_mem(buf, nwords)

    def _rom_flash_ready(self, nwords=4):
        """SAFETY GATE for any ROM erase/program (mirrors debug.py): read flash offset
        0 via esp_rom_spiflash_read and require the first byte to be the ESP image magic
        0xE9. A correct 0xE9 proves the ROM read path is configured; anything else means
        erase/program would target a misconfigured chip, so callers must refuse.
        Read-only. Returns (ready, rom_words, magic)."""
        c = self._chip()
        rom, sram = c.get("rom"), c.get("sram")
        if not rom or not sram or "spiflash_read" not in rom:
            return False, None, None
        dest = sram["data"]
        saved = self.read_mem(dest, nwords)
        try:
            ret, halted = self.call_rom("spiflash_read", args=(0x0, dest, nwords * 4))
            rom_words = self.read_mem(dest, nwords)
            magic = (rom_words[0] & 0xFF) if rom_words else None
            ready = bool(halted) and ret == 0 and magic == 0xE9
            return ready, rom_words, magic
        finally:
            self.write_mem(dest, saved)             # leave scratch byte-identical

    def flash_write(self, addr, data, log=None, verify=True):
        """Program `data` (bytes) to flash byte-offset `addr` via the ROM
        esp_rom_spiflash_* functions, GATED behind _rom_flash_ready() (the 0xE9 magic
        self-test) — refuses to erase/program a chip whose legacy ROM flash state isn't
        configured (brick risk), exactly like the RISC-V path. addr and len(data) must
        be 4-byte aligned. Hart must be halted.

        NOTE (#29): on the S3 the gate currently does not pass on a running app (NULL
        legacy globals + deep-nesting attach — see the class STATUS note), so this
        raises without touching flash until that init is completed."""
        def _log(m):
            if log:
                log(m)
        if len(data) % 4 or addr % 4:
            raise ValueError("flash_write: addr and len(data) must be 4-byte aligned")
        ready, rw, magic = self._rom_flash_ready()
        if not ready and "spiflash_config_param" in self._chip().get("rom", {}):
            _log("  flash_write: gate failed cold — running flash_init (config_param)")
            self.flash_init()
            ready, rw, magic = self._rom_flash_ready()
        if not ready:
            raise RuntimeError(
                "flash_write: ROM spiflash read-back self-test FAILED — the legacy ROM "
                "flash read path is not configured on this S3 target (ROM read "
                f"{rw}, first-byte magic {magic}, want 0xE9). Refusing to erase/program "
                "(brick risk). On the S3 this needs rom_spiflash_legacy_funcs + the "
                "deep-nesting spi_flash_attach, not yet supported by the windowed-call "
                "path (#29) — or flash it with esptool.")
        c = self._chip()
        sram = c["sram"]
        buf = sram["data"]
        words = [int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)]
        _log(f"  flash_write: 0x{addr:08x} +{len(data)}B ({len(words)} words)")
        self.call_rom("spiflash_unlock")
        first = addr & ~0xFFF
        last = (addr + len(data) - 1) & ~0xFFF
        for sa in range(first, last + 1, 0x1000):
            r, _ = self.call_rom("spiflash_erase_sector", args=(sa >> 12,))
            if r != 0:
                raise RuntimeError(f"flash erase sector {sa >> 12} -> {r}")
        chunk_words = 0x400                       # 4 KiB per call (within scratch)
        for w0 in range(0, len(words), chunk_words):
            wchunk = words[w0:w0 + chunk_words]
            self.write_mem(buf, wchunk)
            dest = addr + w0 * 4
            r, _ = self.call_rom("spiflash_write", args=(dest, buf, len(wchunk) * 4))
            if r != 0:
                raise RuntimeError(f"flash write @0x{dest:08x} -> {r}")
        if verify:
            got = self.flash_read_rom(addr, len(words))
            if got != words:
                raise RuntimeError(f"flash_write: verify mismatch @0x{addr:08x}")
            _log("  flash_write: verify OK")
        return True
