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
        # de-assert the debug-int halt request, then resume onto the stub
        self.nar_write(DCRCLR, OCDDCR_DEBUGINTERRUPT)
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
