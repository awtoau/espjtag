"""espjtag.debug — the full RISC-V debugger built on the transport: halt/resume,
GPR/CSR read+write (abstract command), memory read/write (System Bus Access),
diag, and the reset_run() method. EspUsbJtag is the public class most users want.

DM register addresses + field offsets used here are ported from the RISC-V debug
spec via openocd-esp32 src/target/riscv/debug_defines.h (BSD-2-Clause OR
CC-BY-4.0); the reset_run() DMI sequence was captured from OpenOCD's `reset run`
-d3 log. Pinned upstream: ../upstream.lock. Provenance: ../ACKNOWLEDGEMENTS.md.
"""

import time

import usb.util

from .constants import (
    VID, PID,
    DMCONTROL, DMSTATUS, HARTINFO, ABSTRACTCS, COMMAND, DATA0,
    SBCS, SBADDRESS0, SBDATA0, DMI_READ, DMI_WRITE, DMI_NOP,
    SB_SBERROR, SB_SBBUSYERROR,
    DM_DMACTIVE, DM_NDMRESET, DM_ACKHAVERESET, DM_RESUMEREQ, DM_HALTREQ,
    DM_ALLHALTED, DM_ALLRUNNING, DM_ALLHAVERESET,
    DM_ANYRUNNING, DM_ALLRESUMEACK, DM_ALLUNAVAIL,
    ABS_BUSY, ABS_CMDERR,
    CMD_ACCESS_REGISTER, AC_TRANSFER, AC_WRITE, AC_AARSIZE32, AC_POSTEXEC,
    CSR_DCSR, CSR_DPC, REG_GPR_BASE, PROGBUF0,
    DCSR_EBREAK_BITS, EBREAK,
)
from . import chips
from .transport import EspUsbJtagTransport


class EspUsbJtag(EspUsbJtagTransport):
    # === RISC-V Debug Module: halt / resume / examine =====================
    def dm_read(self, addr):
        d, _ = self.dmi_read(addr)
        return d

    def halt(self, timeout=200, disable_wdt=True):
        """Request halt and wait for allhalted. Returns True if halted.

        On success, by default disables the chip's watchdogs (`disable_wdt`) so a
        WDT can't reset the chip out from under the debugger while it's held halted
        — the C6 halt-flakiness fix (probe-rs + OpenOCD both do this). Pass
        disable_wdt=False to leave them running."""
        self.dmi_write(DMCONTROL, DM_HALTREQ | DM_DMACTIVE)
        for _ in range(timeout):
            if self.dm_read(DMSTATUS) & DM_ALLHALTED:
                self.dmi_write(DMCONTROL, DM_DMACTIVE)     # drop haltreq
                if disable_wdt:
                    self._wdt_disable()
                return True
        return False

    def _wdt_disable(self):
        """Disable this chip's watchdogs (TG0/TG1/LP-RTC/super-WDT) so none resets
        the chip while it's halted. Data-driven from chips.py `wdt`; a no-op on a
        chip without a wdt table. Writes the memory-mapped WDT regs via SBA — the
        VERBATIM esp32c6.cfg esp32c6_wdt_disable sequence. Core must be halted."""
        w = self._chip().get("wdt")
        if not w:
            return                                 # untabled chip — leave WDTs alone
        for wkey_reg, cfg_reg, cfg_val in w["disable"]:
            self.write_mem32(wkey_reg, w["key"])   # unlock
            self.write_mem32(cfg_reg, cfg_val)     # zero/feed the config
        for reg, val in w.get("int_clear", ()):
            self.write_mem32(reg, val)             # clear pending WDT int state

    def resume(self, timeout=200):
        self.dmi_write(DMCONTROL, DM_RESUMEREQ | DM_DMACTIVE)
        for _ in range(timeout):
            if self.dm_read(DMSTATUS) & DM_ALLRUNNING:
                self.dmi_write(DMCONTROL, DM_DMACTIVE)
                return True
        return False

    def examine(self):
        """Bring the DM up: dmactive, clear any stale havereset, read dmstatus."""
        self.dmi_write(DMCONTROL, DM_DMACTIVE)
        st = self.dm_read(DMSTATUS)
        if st & DM_ALLHAVERESET:
            self.dmi_write(DMCONTROL, DM_ACKHAVERESET | DM_DMACTIVE)
        return st

    # === Abstract command: read/write GPRs and CSRs =======================
    def _abstract_wait(self, timeout=200):
        for _ in range(timeout):
            cs = self.dm_read(ABSTRACTCS)
            if not (cs & ABS_BUSY):
                err = (cs & ABS_CMDERR) >> 8
                if err:
                    # clear the error (W1C) for the next command
                    self.dmi_write(ABSTRACTCS, ABS_CMDERR)
                return err
        return -1

    def read_register(self, regno):
        """Read a hart register (GPR x1.. = 0x1000+n, CSR = its csr number).
        The hart must be halted."""
        cmd = (CMD_ACCESS_REGISTER | AC_AARSIZE32 | AC_TRANSFER | (regno & 0xFFFF))
        self.dmi_write(COMMAND, cmd)
        self._abstract_wait()
        return self.dm_read(DATA0)

    def write_register(self, regno, value):
        self.dmi_write(DATA0, value & 0xFFFFFFFF)
        cmd = (CMD_ACCESS_REGISTER | AC_AARSIZE32 | AC_TRANSFER | AC_WRITE
               | (regno & 0xFFFF))
        self.dmi_write(COMMAND, cmd)
        return self._abstract_wait()

    # === System Bus Access: memory read/write (no running hart needed) =====
    def _sb_setup(self, size_access=2, autoincrement=False, readondata=False,
                  readonaddr=False):
        # sbcs: sbaccess at [19:17] (2 = 32-bit), sbautoincrement[16],
        # sbreadondata[15], sbreadonaddr[20]
        cs = (size_access << 17)
        if autoincrement:
            cs |= (1 << 16)
        if readondata:
            cs |= (1 << 15)
        if readonaddr:
            cs |= (1 << 20)
        self.dmi_write(SBCS, cs)

    def read_mem32(self, addr):
        """Read one 32-bit word from `addr` via System Bus Access."""
        self._sb_setup(readonaddr=True)
        self.dmi_write(SBADDRESS0, addr & 0xFFFFFFFF)
        return self.dm_read(SBDATA0)

    def write_mem32(self, addr, value):
        """Write one 32-bit word to `addr` via System Bus Access."""
        self._sb_setup()
        self.dmi_write(SBADDRESS0, addr & 0xFFFFFFFF)
        self.dmi_write(SBDATA0, value & 0xFFFFFFFF)

    def _read_mem_slow(self, addr, nwords):
        """The original per-word read_mem: one USB round-trip per word. Kept as a
        proven fallback for when the batched path detects an SBA bus error."""
        self._sb_setup(autoincrement=True, readondata=True, readonaddr=True)
        self.dmi_write(SBADDRESS0, addr & 0xFFFFFFFF)
        out = [self.dm_read(SBDATA0) for _ in range(nwords)]
        self.dmi_write(SBCS, 0)
        return out

    def read_mem(self, addr, nwords):
        """Read nwords 32-bit words from addr via System Bus Access, BATCHED.

        SBA with sbreadonaddr+sbreadondata+sbautoincrement makes the hardware do
        the walking: writing SBADDRESS0 fetches word0 (readonaddr); each READ of
        SBDATA0 returns the current word AND auto-triggers the next bus read +
        bumps the address (readondata+autoincrement). So instead of N separate
        round-trips we issue ONE address write + a PIPELINE of READ(SBDATA0)
        scans in one batched scan stream (chunked at the IN-FIFO limit).

        Two pipelines stack and BOTH must be accounted for:
          * the RISC-V DTM read pipeline — a DMI READ scan returns the PREVIOUS
            DMI access's data, so the data for read #k appears in scan #k+1;
          * the SBA readondata pipeline — reading SBDATA0 yields the current word
            and arms the next.
        Net effect (verified on the bench against the per-word path and OpenOCD
        `mdw`): issue nwords+1 READ(SBDATA0) scans after the address write; the
        word stream lands in capture slots [2 .. 2+nwords). We then read SBCS and
        fall back to the slow per-word path if the burst hit an SBA bus error."""
        if nwords <= 0:
            return []
        self._ensure_dtmcs()
        self._sb_setup(autoincrement=True, readondata=True, readonaddr=True)
        reqs = [(SBADDRESS0, addr & 0xFFFFFFFF, DMI_WRITE)]   # arms word0 fetch
        reqs += [(SBDATA0, 0, DMI_READ)] * (nwords + 1)       # pipeline of reads
        reqs += [(SBDATA0, 0, DMI_NOP)]                       # flush last DTM read
        res = self._dmi_batch(reqs)
        # res[0] is the address write; res[1] is the read-phase that returns the
        # SBADDRESS0-write's (stale) result; res[2..2+nwords) carry word0..wordN-1.
        out = [res[2 + i][0] for i in range(nwords)]
        # Guard: any DTM op-status 3 (busy) in the burst = pipeline stall.
        if any(res[i][1] == 3 for i in range(1, 2 + nwords)):
            self.dmi_write(SBCS, 0)
            return self._read_mem_slow(addr, nwords)
        # Guard: SBA bus error (sberror / sbbusyerror) -> the burst raced the bus.
        sbcs = self.dm_read(SBCS)
        if sbcs & (SB_SBERROR | SB_SBBUSYERROR):
            self.dmi_write(SBCS, SB_SBERROR | SB_SBBUSYERROR)  # W1C clear
            self.dmi_write(SBCS, 0)
            return self._read_mem_slow(addr, nwords)
        self.dmi_write(SBCS, 0)
        return out

    def write_mem(self, addr, words):
        """Write a list of 32-bit `words` starting at `addr` via System Bus Access,
        BATCHED. Mirror of read_mem: arm autoincrement, write SBADDRESS0 once, then
        stream SBDATA0 writes — the hardware bumps the address per write. One handful
        of USB exchanges for the whole block instead of N round-trips. This is the
        data-staging primitive for the flash loader (#3): fill a RAM buffer fast.
        Returns the number of words written. Raises on an SBA bus error (don't
        silently half-write a flash buffer)."""
        words = list(words)
        if not words:
            return 0
        self._ensure_dtmcs()
        self._sb_setup(autoincrement=True)               # sbaccess=32, autoincr
        reqs = [(SBADDRESS0, addr & 0xFFFFFFFF, DMI_WRITE)]
        reqs += [(SBDATA0, w & 0xFFFFFFFF, DMI_WRITE) for w in words]
        res = self._dmi_batch(reqs)
        # any DTM busy in the burst = a stalled write -> redo slowly, correctly.
        if any(r is not None and r[1] == 3 for r in res):
            self._sb_setup(autoincrement=True)
            self.dmi_write(SBADDRESS0, addr & 0xFFFFFFFF)
            for w in words:
                self.dmi_write(SBDATA0, w & 0xFFFFFFFF)
        sbcs = self.dm_read(SBCS)
        if sbcs & (SB_SBERROR | SB_SBBUSYERROR):
            self.dmi_write(SBCS, SB_SBERROR | SB_SBBUSYERROR)   # W1C clear
            self.dmi_write(SBCS, 0)
            raise RuntimeError(f"write_mem SBA bus error at 0x{addr:08x} "
                               f"(sbcs=0x{sbcs:08x})")
        self.dmi_write(SBCS, 0)
        return len(words)

    # === "call a function on the target" — the flash-loader call primitive (#3) ==
    # A halted RISC-V hart can be made to run an arbitrary on-chip routine (a ROM
    # entry point, or code we staged in RAM) by the standard debug recipe:
    #   * stage args in a0..a7 (GPR x10..x17), sp in a scratch stack, ra at a
    #     scratch SRAM word holding `ebreak` (the return trap);
    #   * set dpc to the entry, make sure dcsr.ebreak* is set so the ebreak at `ra`
    #     re-enters debug mode, resume, and poll for the halt;
    #   * read a0 for the return value.
    # We SAVE and RESTORE every register we clobber (a0..aN, sp=x2, ra=x1, dpc,
    # dcsr) so the interrupted app is left byte-identical and can resume cleanly.
    # The hart MUST already be halted (call halt() first).
    _ABI_ARG_GPR = (10, 11, 12, 13, 14, 15, 16, 17)        # a0..a7 = x10..x17

    def call_function(self, entry, args=(), stack=None, trap=None,
                      timeout=4000, restore=True):
        """Call on-target code at `entry` with up to 8 integer `args` (placed in
        a0..a7), returning a0. `stack` = an SP value (a scratch SRAM top, grows
        down); `trap` = address of an SRAM word we set to `ebreak` and point ra at,
        so the callee's `ret` traps back into debug mode. The hart must be halted.

        Saves/restores ra, sp, dpc, dcsr and the clobbered arg GPRs (restore=True)
        so the live app is undisturbed. Returns (a0, halted) — halted=False means
        the callee did not trap within `timeout` polls (left halted; inspect)."""
        if len(args) > len(self._ABI_ARG_GPR):
            raise ValueError("call_function: at most 8 integer args")
        # --- save state we're about to clobber ---
        saved = {}
        if restore:
            saved["dpc"] = self.read_register(CSR_DPC)
            saved["dcsr"] = self.read_register(CSR_DCSR)
            saved["ra"] = self.read_register(REG_GPR_BASE + 1)
            saved["sp"] = self.read_register(REG_GPR_BASE + 2)
            for i in range(len(args)):
                g = self._ABI_ARG_GPR[i]
                saved[g] = self.read_register(REG_GPR_BASE + g)
        # --- ebreak return-trap: write `ebreak` to the trap word, ra -> it ---
        self.write_mem32(trap, EBREAK)
        self.write_register(REG_GPR_BASE + 1, trap)            # ra = trap
        if stack is not None:
            self.write_register(REG_GPR_BASE + 2, stack)       # sp = scratch top
        for i, a in enumerate(args):
            self.write_register(REG_GPR_BASE + self._ABI_ARG_GPR[i], a & 0xFFFFFFFF)
        # --- ensure an ebreak in M/U mode enters debug (don't trap to the app) ---
        dcsr = self.read_register(CSR_DCSR)
        if (dcsr | DCSR_EBREAK_BITS) != dcsr:
            self.write_register(CSR_DCSR, dcsr | DCSR_EBREAK_BITS)
        # --- jump there: dpc = entry, resume, wait for the trap halt ---
        self.write_register(CSR_DPC, entry)
        # resume handshake (mirrors _resume_go): set resumereq, wait for the hart
        # to ack it is running, THEN clear resumereq — otherwise the request can
        # race and never take. Then poll for the ebreak re-halt.
        self.dmi_write(DMCONTROL, DM_RESUMEREQ | DM_DMACTIVE)
        for _ in range(256):
            if self.dm_read(DMSTATUS) & (DM_ALLRESUMEACK | DM_ALLRUNNING):
                break
        self.dmi_write(DMCONTROL, DM_DMACTIVE)                 # resumereq <- 0
        halted = False
        for _ in range(timeout):
            if self.dm_read(DMSTATUS) & DM_ALLHALTED:
                halted = True
                break
        ret = self.read_register(REG_GPR_BASE + 10) if halted else None   # a0
        # --- restore the app's registers so resume() continues it cleanly ---
        if restore and halted:
            for g, v in saved.items():
                if g == "dpc":
                    self.write_register(CSR_DPC, v)
                elif g == "dcsr":
                    self.write_register(CSR_DCSR, v)
                elif g == "ra":
                    self.write_register(REG_GPR_BASE + 1, v)
                elif g == "sp":
                    self.write_register(REG_GPR_BASE + 2, v)
                else:
                    self.write_register(REG_GPR_BASE + g, v)
        return ret, halted

    # === flash over JTAG — Option A: call the ROM esp_rom_spiflash_* (#3) ========
    # Build on call_function + the per-chip ROM/SRAM table (chips.py). The whole
    # path is GATED behind a read-back self-test (_rom_flash_ready): we will not
    # erase or program unless a ROM read of a known region matches the XIP window,
    # because the legacy ROM g_rom_spiflash_chip state is NOT guaranteed to be
    # configured in a running app (measured: in the Zephyr app it is NOT — ROM
    # reads return constant garbage), and on the C6 ECO0 the ROM read/write path
    # needs revision-specific workarounds (see bootloader_flash.c rom_read_api_
    # workaround). Erasing/programming through a misconfigured ROM state could
    # corrupt the wrong flash region. So flash_write() refuses on a board that
    # doesn't pass the gate rather than risk a brick.

    def _chip(self):
        return chips.lookup(self.read_idcode()) or {}

    def call_rom(self, sym, args=(), restore=True):
        """Call a named ROM function from this chip's chips.rom table (e.g.
        'spiflash_erase_sector') with integer args. The hart must be halted and a
        scratch SRAM window must be tabled. Returns (a0, halted)."""
        c = self._chip()
        rom, sram = c.get("rom"), c.get("sram")
        if not rom or sym not in rom:
            raise RuntimeError(f"call_rom: no ROM symbol {sym!r} tabled for "
                               f"{c.get('name', '?')}")
        if not sram:
            raise RuntimeError("call_rom: no scratch SRAM window tabled")
        return self.call_function(rom[sym], args=args, stack=sram["stack"],
                                  trap=sram["trap"], restore=restore)

    def flash_read_xip(self, off, nwords):
        """Read nwords of flash via the cache-mapped XIP window (chips flash_xip +
        off). This is the WORKING read path (verified on the bench); the ROM
        esp_rom_spiflash_read needs init the running app may not have done. Note the
        XIP window maps the app's active flash mapping, so `off` is an offset INTO
        that mapping, not necessarily a raw flash byte address. Hart should be
        halted with icache enabled."""
        c = self._chip()
        xip = c.get("flash_xip")
        if xip is None:
            raise RuntimeError("flash_read_xip: no flash_xip window tabled")
        return self.read_mem(xip + off, nwords)

    def flash_read_rom(self, addr, nwords):
        """Read nwords from RAW flash byte-offset `addr` via the ROM
        esp_rom_spiflash_read (needs flash_init done / the gate passing). Unlike
        flash_read_xip this is a TRUE raw flash offset (XIP maps the app's mapping,
        not raw flash), so it's the correct read for verify + inspection. Hart
        halted; icache toggled. Reuses the scratch staging buffer."""
        sram = self._chip()["sram"]
        buf = sram["data"]
        self.call_rom("cache_disable_icache")
        self.call_rom("spiflash_read", args=(addr, buf, nwords * 4))
        words = self.read_mem(buf, nwords)
        self.call_rom("cache_enable_icache")
        return words

    def flash_init(self, chip_size=0x1000000):
        """Repopulate the legacy ROM spiflash geometry (g_rom_spiflash_chip) so the
        esp_rom_spiflash_* helpers work on a board whose running app left it unset.
        The flash is already pin/mode-attached (XIP is live), so we only restore the
        geometry — esp_rom_spiflash_config_param(devid=0, chip_size, 64 KiB block,
        4 KiB sector, 256 B page, 0xFFFF status mask). Hart must be halted. Returns
        the ROM result (0 = OK). This is the un-gate for flash_write (#30)."""
        rom = self._chip().get("rom", {})
        if "spiflash_config_param" not in rom:
            raise RuntimeError("flash_init: spiflash_config_param not tabled for this chip")
        # attach first — sets the legacy funcs' dummy cycles/read mode (without it the
        # ROM read returns garbage on a running app). ishspi=0 (default GPIO), legacy=0.
        if "spi_flash_attach" in rom:
            self.call_rom("spi_flash_attach", args=(0, 0))
        r, _ = self.call_rom("spiflash_config_param",
                             args=(0, chip_size, 0x10000, 0x1000, 0x100, 0xFFFF))
        return r

    def _rom_flash_ready(self, nwords=4):
        """SAFETY GATE for any ROM erase/program. Read flash offset 0 via the ROM
        esp_rom_spiflash_read and require the first byte to be the ESP image magic
        0xE9 — the 2nd-stage bootloader header sits at flash 0 on every ESP target.
        A correct 0xE9 proves the ROM read path is configured (attach dummy cycles +
        config_param geometry); garbage (the running app left the controller in
        fast-XIP mode) means erase/program would target a misconfigured chip, so we
        refuse (brick risk). (Earlier this compared against flash_read_xip, but the
        XIP window maps the app's region, not raw flash 0 — never a valid reference.)
        Read-only; icache toggled around the ROM read. Returns (ready, rom_words, magic)."""
        c = self._chip()
        rom, sram = c.get("rom"), c.get("sram")
        if not rom or not sram or "spiflash_read" not in rom:
            return False, None, None
        dest = sram["data"]
        saved = self.read_mem(dest, nwords)
        try:
            self.call_rom("cache_disable_icache")
            ret, halted = self.call_rom("spiflash_read", args=(0x0, dest, nwords * 4))
            rom_words = self.read_mem(dest, nwords)
            self.call_rom("cache_enable_icache")
            magic = (rom_words[0] & 0xFF) if rom_words else None
            ready = bool(halted) and ret == 0 and magic == 0xE9
            return ready, rom_words, magic
        finally:
            self.write_mem(dest, saved)         # leave scratch byte-identical

    def flash_write(self, addr, data, log=None, verify=True):
        """Program `data` (bytes) to flash byte-offset `addr` via the C6 ROM
        esp_rom_spiflash_* functions (Option A, #3): unlock, erase the covering
        4 KiB sectors, stage each chunk into scratch SRAM (batched write_mem), and
        esp_rom_spiflash_write it, then (verify) read back.

        *** SAFETY: refuses to run unless _rom_flash_ready() passes. *** The legacy
        ROM flash state is not guaranteed configured in a running app, so this gate
        prevents erasing/programming a misconfigured chip (brick risk). On a board
        that hasn't set up the ROM spiflash globals (e.g. the Zephyr app, measured),
        this raises WITHOUT touching flash. The hart must be halted first.

        NOTE: addr and len(data) must be 4-byte aligned (ROM write requirement);
        erase is sector-granular so `addr` should be 4 KiB-aligned for a clean
        program. UNVERIFIED end-to-end on the bench — see docs/JTAG-FLASH-WRITES.md.
        """
        def _log(m):
            if log:
                log(m)
        if len(data) % 4 or addr % 4:
            raise ValueError("flash_write: addr and len(data) must be 4-byte aligned")
        ready, rw, xw = self._rom_flash_ready()
        if not ready and "spiflash_config_param" in self._chip().get("rom", {}):
            _log("  flash_write: gate failed cold — running flash_init (config_param)")
            self.flash_init()                       # repopulate ROM geometry, retry
            ready, rw, xw = self._rom_flash_ready()
        if not ready:
            raise RuntimeError(
                "flash_write: ROM spiflash read-back self-test FAILED — the legacy "
                "ROM flash read path is not configured on this target (ROM read "
                f"{rw}, first-byte magic {xw}, want 0xE9). Refusing to erase/program "
                "(brick risk). flash_init runs spi_flash_attach + config_param; if "
                "the magic is still wrong this chip/revision needs more (config_clk/"
                "readmode or an ECO workaround — see #33), or flash it with esptool.")
        c = self._chip()
        sram = c["sram"]
        buf = sram["data"]
        words = [int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)]
        _log(f"  flash_write: 0x{addr:08x} +{len(data)}B ({len(words)} words)")
        # unlock once
        self.call_rom("spiflash_unlock")
        # erase covering sectors
        first = addr & ~0xFFF
        last = (addr + len(data) - 1) & ~0xFFF
        for sa in range(first, last + 1, 0x1000):
            r, _ = self.call_rom("spiflash_erase_sector", args=(sa >> 12,))
            if r != 0:
                raise RuntimeError(f"flash erase sector {sa >> 12} -> {r}")
        # stage + program in chunks bounded by the scratch buffer headroom
        chunk_words = 0x400                       # 4 KiB per call (well within scratch)
        for w0 in range(0, len(words), chunk_words):
            wchunk = words[w0:w0 + chunk_words]
            self.write_mem(buf, wchunk)
            dest = addr + w0 * 4
            r, _ = self.call_rom("spiflash_write", args=(dest, buf, len(wchunk) * 4))
            if r != 0:
                raise RuntimeError(f"flash write @0x{dest:08x} -> {r}")
        if verify:
            got = self.flash_read_rom(addr, len(words))   # raw ROM read (true offset)
            if got != words:
                raise RuntimeError(f"flash_write: verify mismatch @0x{addr:08x}")
            _log("  flash_write: verify OK")
        return True

    def diag(self, log=print):
        """Verbose read-only dump of the debug module — useful BEFORE resetting."""
        idcode = self.read_idcode()
        log(f"  IDCODE        = 0x{idcode:08x}")
        dmcontrol, st1 = self.dmi_read(DMCONTROL)
        log(f"  dmcontrol@0x10= 0x{dmcontrol:08x} (op={st1}) "
            f"dmactive={dmcontrol & 1} ndmreset={(dmcontrol >> 1) & 1}")
        dmstatus, st2 = self.dmi_read(DMSTATUS)
        log(f"  dmstatus@0x11 = 0x{dmstatus:08x} (op={st2}) "
            f"version={dmstatus & 0xF} allhalted={(dmstatus >> 9) & 1} "
            f"allrunning={(dmstatus >> 11) & 1}")
        hartinfo, _ = self.dmi_read(HARTINFO)
        log(f"  hartinfo@0x12 = 0x{hartinfo:08x}")
        return idcode, dmcontrol, dmstatus


    def reset_run(self, log=None):
        """Full-system reset that re-samples the BOOT strap, then RUN the app —
        mirrors OpenOCD's `reset run` DMI sequence captured from its -d3 log:
            dmi_write 0x10 0x40000001   (resumereq | dmactive — pre-reset)
            dmi_write 0x10 0x80000003   (haltreq | ndmreset | dmactive — assert)
        then deassert ndmreset and resume. The ndmreset is the full-system reset
        the C6 USJ core-reset can't do; haltreq holds the hart so the reset is
        clean; resumereq runs the freshly-strapped app. Verified vs OpenOCD."""
        if log:
            log("  -- pre-reset diag --")
            self.diag(log)
        self.dmi_write(DMCONTROL, DM_DMACTIVE)                          # claim DM
        self.dmi_write(DMCONTROL, DM_RESUMEREQ | DM_DMACTIVE)           # 0x40000001
        self.dmi_write(DMCONTROL, DM_HALTREQ | DM_NDMRESET | DM_DMACTIVE)  # 0x80000003
        time.sleep(0.05)
        self.dmi_write(DMCONTROL, DM_HALTREQ | DM_DMACTIVE)             # deassert ndmreset, keep halted
        self.dmi_write(DMCONTROL, DM_ACKHAVERESET | DM_DMACTIVE)        # ack the reset
        self.dmi_write(DMCONTROL, DM_RESUMEREQ | DM_DMACTIVE)           # RUN
        self.dmi_write(DMCONTROL, 0)                                    # release DM
        usb.util.dispose_resources(self.dev)

    # === Faithful OpenOCD `reset run` building blocks (riscv-013.c) =========
    # These mirror the riscv013_* helpers OpenOCD runs around its reset, which the
    # captured 12-write list omitted (it logged only the dmi_write *commands*, not
    # the stateful halt/dcsr/resume polls). Ported by LOGIC, not literal C.

    def _halt_go(self, timeout=256):
        """riscv013_halt_go: set haltreq, poll dmstatus until no hart is running,
        then drop haltreq. Returns True if halted."""
        self.dmi_write(DMCONTROL, DM_HALTREQ | DM_DMACTIVE)
        ok = False
        for _ in range(timeout):
            if not (self.dm_read(DMSTATUS) & DM_ANYRUNNING):
                ok = True
                break
        self.dmi_write(DMCONTROL, DM_DMACTIVE)            # haltreq <- 0
        return ok

    def _set_dcsr_ebreak(self):
        """set_dcsr_ebreak(step=false): read dcsr, OR in ebreakm|ebreaku, write it
        back (only if changed). On the C6 this turns 0xc3 into 0x90c3 — exactly
        what OpenOCD's -d3 trace shows (dcsr <- 0x90c3). The hart must be halted."""
        dcsr = self.read_register(CSR_DCSR)
        new = dcsr | DCSR_EBREAK_BITS
        if new != dcsr:
            self.write_register(CSR_DCSR, new)
        return dcsr, new

    def _resume_go(self, timeout=256):
        """riscv013_step_or_resume_current_hart(step=false): set resumereq, poll
        dmstatus until allresumeack (fail on allunavail), then clear resumereq.
        Returns True on a clean resume. The hart must be halted first."""
        self.dmi_write(DMCONTROL, DM_RESUMEREQ | DM_DMACTIVE)
        for _ in range(timeout):
            st = self.dm_read(DMSTATUS)
            if st & DM_ALLUNAVAIL:
                self.dmi_write(DMCONTROL, DM_DMACTIVE)
                return False
            if st & DM_ALLRESUMEACK:
                self.dmi_write(DMCONTROL, DM_DMACTIVE)    # resumereq <- 0
                return True
        self.dmi_write(DMCONTROL, DM_DMACTIVE)
        return False

    def _c6_soc_reset(self):
        """esp32c6_soc_reset (openocd-esp32 tcl/target/esp32c6.cfg, run from the
        reset-assert-post event): the ESP32-C6-specific assert. Halt the hart,
        then via System Bus Access poke the two LP_AON software-reset registers,
        clearing dmactive between them to drop SBA's sbbusy (or the DM wedges).
        Ends by re-asserting haltreq|ndmreset (0x80000003) to hold the hart in
        reset — exactly the captured 12 writes, in order.

        The two LP_AON register addresses come from the per-chip table
        (espjtag.chips, #4), keyed by the live target IDCODE — not hardcoded here.
        Raises if the connected part has no tabled reset regs (don't blindly poke
        another chip's address space)."""
        rst = chips.reset_for(self.read_idcode())
        if not rst:
            raise RuntimeError(
                "_c6_soc_reset: no tabled SoC reset registers for this IDCODE "
                f"0x{self.read_idcode():08x} — refusing to guess reset addresses")
        self.dmi_write(DMCONTROL, DM_HALTREQ | DM_DMACTIVE)              # 0x80000001
        # LP_AON_SYS_CFG = HPSYS_SW_RESET  (write 0x80000000 to 0x600b1034 via SBA)
        self.write_mem32(rst["hpsys_cfg"], rst["hpsys_sw_reset"])
        self.dmi_write(DMCONTROL, 0)                                     # clear sbbusy
        # LP_AON_CPUCORE0_CFG = CPU_CORE0_SW_RESET (0x10000000 to 0x600b1038)
        self.write_mem32(rst["cpucore0_cfg"], rst["cpucore0_sw_reset"])
        self.dmi_write(DMCONTROL, 0)                                     # clear sbbusy
        self.dmi_write(DMCONTROL, DM_RESUMEREQ | DM_DMACTIVE)            # 0x40000001
        time.sleep(0.01)                                                 # OpenOCD `sleep 10` (ms): let the SW reset propagate before re-asserting
        self.dmi_write(DMCONTROL, DM_RESUMEREQ | DM_DMACTIVE)            # 0x40000001 (clear allhalted)
        self.dmi_write(DMCONTROL, DM_HALTREQ | DM_NDMRESET | DM_DMACTIVE)  # 0x80000003 hold in reset

    def _deassert_reset(self, timeout=256):
        """riscv_deassert_reset: clear ndmreset (write dmactive, haltreq=0 for a
        `run`), poll dmstatus until the hart has left reset (NOT allunavail-without-
        havereset), then ackhavereset."""
        self.dmi_write(DMCONTROL, DM_DMACTIVE)            # ndmreset <- 0, haltreq <- 0
        for _ in range(timeout):
            st = self.dm_read(DMSTATUS)
            if not (st & DM_ALLUNAVAIL) or (st & DM_ALLHAVERESET):
                break
        self.dmi_write(DMCONTROL, DM_ACKHAVERESET | DM_DMACTIVE)

    def reset_run_from_rom(self, log=None):
        """Boot a freshly-flashed ESP32-C6 OUT of post-flash USB-Serial/JTAG ROM
        download mode and into its app — the case the plain reset_run() can't do.

        WHY reset_run() isn't enough (verified on the bench): when esptool leaves
        the chip with `--after no-reset`, the C6 is held in download mode by the
        BOOT-strap being *sampled LOW*. Per Espressif's USB-Serial/JTAG console
        guide, the USJ can only trigger a CORE reset, which does NOT re-sample the
        strap — so a bare ndmreset (or even OpenOCD's pure-JTAG `reset run`)
        re-enters the ROM downloader. Measured: ndmreset-only and OpenOCD `reset
        run` both boot 0/3 from this state; the core just lands back at the ROM
        reset vector with download still latched.

        What DOES clear the strap latch is a USB *bus* reset, which re-enumerates
        the USJ peripheral. But a USB reset ALONE is also 0/3 (the core is still
        parked in esptool's download stub). The reliable boot, proven 3/3 on
        xiao-c6-b, is the COMBINATION, in this order:

            1. USB bus reset      -> re-enumerate USJ, clear the download latch
            2. ndmreset + resume  -> restart the core so the just-re-strapped ROM
                                     boots from flash instead of the stub

        The USB bus reset goes through the cross-platform helper
        espjtag.usbreset.reset_device (pyusb dev.reset() == libusb_reset_device;
        on Linux that is the SAME USBDEVFS_RESET ioctl this flow was proven with —
        see usbreset.py and docs/CROSS-PLATFORM-USB.md for the per-OS behaviour).

        == REQUIRED BENCH RE-VERIFICATION (espjtag #13) =======================
        The 3/3 ROM-boot result was measured with the OLD code path on Linux.
        Routing the reset through usbreset.reset_device does NOT change the
        underlying call on Linux (still USBDEVFS_RESET via libusb), so the strap-
        clear SHOULD be unchanged — but this MUST be re-verified on the bench
        (flash a C6 with esptool --after no-reset, then reset_run_from_rom, and
        confirm it boots the app, repeated for confidence) before this path is
        trusted. The USB reset is load-bearing for the strap clear; do NOT assume
        it boots untested. macOS is expected NOT to work (libusb_reset_device is a
        documented silent no-op there); Windows is untested.
        ======================================================================

        Around the ndmreset we run OpenOCD's faithful reset-run handshake (ported
        from openocd-esp32 riscv-013.c + esp32c6.cfg): the esp32c6_soc_reset SBA
        writes (assert), riscv_deassert_reset's poll+ackhavereset, then the
        halt_set_dcsr_ebreak step OpenOCD does whenever a hart spontaneously
        resets — halt_go, dcsr <- 0x90c3 (ebreakm|ebreaku), and the resume
        handshake (resumereq, poll allresumeack, clear resumereq).

        Returns True on a clean resume. Leaves the app running. The USB reset
        invalidates this object's handle, so a NEW transport is opened internally
        for the JTAG phase; the caller's `self.dev` is disposed."""
        from .usbreset import reset_device, platform_reset_note

        def _log(m):
            if log:
                log(m)

        # --- phase 1: USB bus reset to clear the BOOT-strap-LOW download latch ---
        # Remember how to re-find this exact unit (the reset re-enumerates it).
        # (bus, port_numbers) is the stable identity that SURVIVES a bus reset —
        # the bus address changes, the physical port path does not.
        bus = self.dev.bus
        ports = tuple(self.dev.port_numbers or ())
        _log(f"  reset_run_from_rom: USB bus reset (clears C6 strap latch) "
             f"on bus {bus} ports {ports}")
        _log(f"    {platform_reset_note()}")
        reset_device(self.dev, log=_log)             # cross-platform; re-enumerates
        usb.util.dispose_resources(self.dev)

        # --- phase 2: reopen JTAG on the same unit and run the reset handshake ---
        # Re-enumeration takes a moment; retry the open until the unit is back.
        # 100 tries x ~the open's own latency covers the USJ re-enumeration with
        # margin (measured back in well under a second); each failed open is the
        # device simply not on the bus yet, so we just try again.
        j = None
        last = None
        for _ in range(100):
            try:
                j = EspUsbJtag._reopen(bus, ports)
                break
            except Exception as e:                   # noqa: BLE001
                last = e
        if j is None:
            raise RuntimeError(f"reset_run_from_rom: USJ did not re-enumerate "
                               f"after USB reset (last: {last})")

        _log("  reset_run_from_rom: JTAG back; running OpenOCD-faithful reset run")
        j.examine()
        j._c6_soc_reset()                            # assert (esp32c6_soc_reset)
        j._deassert_reset()                          # leave reset, ackhavereset
        # halt_set_dcsr_ebreak: the hart just reset -> halt, set dcsr.ebreak, resume
        j._halt_go()
        old, new = j._set_dcsr_ebreak()
        _log(f"    dcsr 0x{old:08x} -> 0x{new:08x}")
        ran = j._resume_go()
        _log(f"    resume {'ACKed (app running)' if ran else 'did NOT ack'}")
        j.dmi_write(DMCONTROL, 0)                    # release DM
        usb.util.dispose_resources(j.dev)
        return ran

    @classmethod
    def _reopen(cls, bus, ports):
        """Open the EspUsbJtag for the unit at (bus, port_numbers) — used to
        re-acquire THIS device after a USB bus reset re-enumerated it. Builds a
        usb_path string the transport's matcher understands ("bus-p.p.p")."""
        usb_path = f"{bus}-" + ".".join(str(p) for p in ports) if ports else None
        return cls(usb_path)


def diag(usb_path=None, log=print):
    """Read-only RISC-V Debug Module dump (IDCODE + dmcontrol/dmstatus/hartinfo)
    over the built-in USB-JTAG — no reset, no side effects on the running app."""
    return EspUsbJtag(usb_path).diag(log=log)


def selftest(usb_path=None, rounds=3):
    """Verify the JTAG stack against a live ESP RISC-V part (C5/C6/...): IDCODE,
    DTMCS, and a dmstatus DMI read must read sane, deterministic values `rounds`
    times. Chip-agnostic — recognises the target TAP from its IDCODE via the
    per-chip table (espjtag.chips, #4), then checks DTMCS version==1 + the table's
    expected abits and a valid dmstatus (version 2/3, op 0). Returns (passed,
    total). Read-only — safe against a board running the app."""
    passed = 0
    for r in range(rounds):
        j = EspUsbJtag(usb_path)
        ic = j.read_idcode()
        dt = j.read_dtmcs()
        ds, st = j.dmi_read(DMSTATUS)
        exp_abits = chips.abits_for(ic)
        dmver = ds & 0xF
        ok = (exp_abits is not None and (dt & 0xF) == 1 and j.abits == exp_abits
              and st == 0 and dmver in (2, 3))
        passed += ok
        usb.util.dispose_resources(j.dev)
        chip = chips.name_for(ic) or "??"
        print(f"  round {r}: [{chip}] IDCODE=0x{ic:08x} DTMCS=0x{dt:08x} "
              f"dmstatus=0x{ds:08x}(st{st} v{dmver})  {'PASS' if ok else 'FAIL'}")
    print(f"  selftest: {passed}/{rounds} passed")
    return passed, rounds

