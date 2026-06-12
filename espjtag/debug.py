"""espjtag.debug — the full RISC-V debugger built on the transport: halt/resume,
GPR/CSR read+write (abstract command), memory read/write (System Bus Access),
diag, and the reset_run() method. EspUsbJtag is the public class most users want.

DM register addresses + field offsets used here are ported from the RISC-V debug
spec via openocd-esp32 src/target/riscv/debug_defines.h (BSD-2-Clause OR
CC-BY-4.0); the reset_run() DMI sequence was captured from OpenOCD's `reset run`
-d3 log. Pinned upstream: ../upstream.lock. Provenance: ../ACKNOWLEDGEMENTS.md.
"""

import time
import zlib

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
    CSR_DCSR, CSR_DPC, CSR_MSTATUS, MSTATUS_MIE, REG_GPR_BASE, PROGBUF0,
    DCSR_EBREAK_BITS, EBREAK,
)
from . import chips
from .transport import EspUsbJtagTransport


def _crc32_le_raw(data, crc=0):
    """Reflected CRC-32 (poly 0xEDB88320), raw accumulation, no pre/post inversion — a
    host mirror of the ESP ROM crc32_le primitive, used to calibrate the host digest
    convention against the live ROM (see EspUsbJtag._crc_host)."""
    crc &= 0xFFFFFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if (crc & 1) else 0)
    return crc & 0xFFFFFFFF


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
        The hart must be halted. RAISES on an abstract-command error — returning
        stale DATA0 as if it were the register poisoned everything downstream
        when a hart was unexpectedly running (#33 diagnosis)."""
        cmd = (CMD_ACCESS_REGISTER | AC_AARSIZE32 | AC_TRANSFER | (regno & 0xFFFF))
        self.dmi_write(COMMAND, cmd)
        err = self._abstract_wait()
        if err:
            raise RuntimeError(f"read_register 0x{regno:x}: abstract cmderr={err} "
                               "(hart running? unsupported reg?)")
        return self.dm_read(DATA0)

    def write_register(self, regno, value):
        self.dmi_write(DATA0, value & 0xFFFFFFFF)
        cmd = (CMD_ACCESS_REGISTER | AC_AARSIZE32 | AC_TRANSFER | AC_WRITE
               | (regno & 0xFFFF))
        self.dmi_write(COMMAND, cmd)
        err = self._abstract_wait()
        if err:
            raise RuntimeError(f"write_register 0x{regno:x}: abstract cmderr={err} "
                               "(hart running? unsupported reg?)")
        return err

    # --- batched register access: one USB stream instead of ~1 ms per access.
    # Register-transfer abstract commands complete within the DTM-advertised idle
    # cycles _dmi_batch already inserts per scan (the same trust OpenOCD's batch
    # path places in dtmcs.idle); any overrun shows up as DTM busy in the batch
    # statuses or cmderr in the trailing ABSTRACTCS check, and we redo the whole
    # thing through the slow per-register path — never trust a dirty batch.
    def write_registers(self, pairs):
        """Write [(regno, value), ...] in ONE batched DMI stream."""
        reqs = []
        for regno, value in pairs:
            reqs.append((DATA0, value & 0xFFFFFFFF, DMI_WRITE))
            reqs.append((COMMAND, CMD_ACCESS_REGISTER | AC_AARSIZE32 | AC_TRANSFER
                         | AC_WRITE | (regno & 0xFFFF), DMI_WRITE))
        res = self._dmi_batch(reqs)
        if any(r is not None and r[1] != 0 for r in res) or self._abstract_wait():
            for regno, value in pairs:
                self.write_register(regno, value)

    def read_registers(self, regnos):
        """Read [regno, ...] in ONE batched DMI stream. Per register the stream is
        [COMMAND=transfer-read, READ(DATA0)]; the DTM read pipeline returns scan
        k's data in scan k+1's capture, so reg k's value lands in slot 2k+2 (the
        next COMMAND write / the trailing NOP)."""
        reqs = []
        for regno in regnos:
            reqs.append((COMMAND, CMD_ACCESS_REGISTER | AC_AARSIZE32 | AC_TRANSFER
                         | (regno & 0xFFFF), DMI_WRITE))
            reqs.append((DATA0, 0, DMI_READ))
        reqs.append((DATA0, 0, DMI_NOP))                    # flush the last read
        res = self._dmi_batch(reqs)
        if any(r is not None and r[1] != 0 for r in res) or self._abstract_wait():
            return [self.read_register(r) for r in regnos]
        return [res[2 * k + 2][0] for k in range(len(regnos))]

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
        # --- save state we're about to clobber (ONE batched read stream) ---
        # dcsr/mstatus are read regardless: the setup decisions below need them.
        save_regs = [CSR_DPC, CSR_DCSR, CSR_MSTATUS,
                     REG_GPR_BASE + 1, REG_GPR_BASE + 2]
        save_regs += [REG_GPR_BASE + self._ABI_ARG_GPR[i] for i in range(len(args))]
        vals = self.read_registers(save_regs)
        saved = dict(zip(save_regs, vals)) if restore else {}
        dcsr, mstatus = vals[1], vals[2]
        # --- ebreak return-trap + args + dpc (ONE batched write stream) ---
        self.write_mem32(trap, EBREAK)
        wr = [(REG_GPR_BASE + 1, trap)]                        # ra = trap
        if stack is not None:
            wr.append((REG_GPR_BASE + 2, stack))               # sp = scratch top
        wr += [(REG_GPR_BASE + self._ABI_ARG_GPR[i], a & 0xFFFFFFFF)
               for i, a in enumerate(args)]
        # ebreak -> debug mode, AND force prv=3: dcsr.prv is the privilege the hart
        # RESUMES into. If the app was halted in U-mode (dcsr.prv=0 — e.g. a Zephyr
        # user thread), the ROM callee would run unprivileged and fault/reset the
        # chip on the spot (#33 cycle-deterministic failure, dcsr.cause=5). The
        # saved dcsr restores the app's own privilege afterwards.
        want_dcsr = dcsr | DCSR_EBREAK_BITS | 0x3
        if want_dcsr != dcsr:
            wr.append((CSR_DCSR, want_dcsr))
        # MASK INTERRUPTS for the callee (what OpenOCD/probe-rs do for algorithm
        # runs): once the app is past early boot, a pending timer/radio interrupt
        # otherwise hijacks the resume at the first instruction — the callee never
        # runs, the trap never fires, the hart is left running (#33 on the C5).
        # The saved mstatus restores the app's interrupt state afterwards.
        if mstatus & MSTATUS_MIE:
            wr.append((CSR_MSTATUS, mstatus & ~MSTATUS_MIE))
        wr.append((CSR_DPC, entry))                            # jump target
        self.write_registers(wr)
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
        if not halted:
            # The callee never trapped (e.g. ROM spiflash_* spinning on a wedged
            # SPI1 when the app was halted mid-flash-IO — #33). Previously we left
            # the hart RUNNING the stuck callee, which poisoned every subsequent
            # abstract command in the session. Re-halt, restore the app's
            # registers, and fail CLEANLY so the caller can attempt recovery
            # (flash_init re-attaches the controller).
            self.dmi_write(DMCONTROL, DM_HALTREQ | DM_DMACTIVE)
            for _ in range(256):
                if self.dm_read(DMSTATUS) & DM_ALLHALTED:
                    break
            self.dmi_write(DMCONTROL, DM_DMACTIVE)
            if restore and (self.dm_read(DMSTATUS) & DM_ALLHALTED):
                self.write_registers(list(saved.items()))
            return None, False
        ret = self.read_register(REG_GPR_BASE + 10)                       # a0
        # --- restore the app's registers so resume() continues it cleanly ---
        if restore:
            self.write_registers(list(saved.items()))          # one batched stream
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

    _FLASH_VENDORS = {0x20: "XMC", 0xEF: "Winbond", 0xC8: "GigaDevice",
                      0x85: "Puya", 0x68: "Boya", 0xA1: "Fudan", 0x0B: "XTX",
                      # 0x46: JEP106 says Silicon Spice (defunct telecom) — the
                      # real holder is an unregistered fab; identified by SFDP
                      # fingerprint + behaviour only (docs/FLASH-DIE-SURVEY.md)
                      0x46: "unregistered-0x46"}
    # SPI1 (the non-cache flash controller) user-command registers — identical
    # layout + base on C5/C6 (DR_REG_SPI1_BASE, spi_mem_reg.h).
    _SPI1 = 0x60003000
    _SPI_CMD, _SPI_USER, _SPI_USER2, _SPI_MISO_DLEN, _SPI_W0 = \
        _SPI1 + 0x0, _SPI1 + 0x18, _SPI1 + 0x20, _SPI1 + 0x28, _SPI1 + 0x58
    _SPI_USR = 1 << 18                       # CMD.usr: start user transaction
    _SPI_USR_COMMAND, _SPI_USR_MISO = 1 << 31, 1 << 28

    _SPI_ADDR, _SPI_USER1 = _SPI1 + 0x4, _SPI1 + 0x1C
    _SPI_USR_ADDR, _SPI_USR_DUMMY = 1 << 30, 1 << 29

    def flash_sfdp(self, addr, nbytes):
        """Read `nbytes` of the SFDP space (JESD216, cmd 0x5A + 24-bit address +
        8 dummy cycles) over JTAG via SPI1 registers. Returns bytes."""
        out = bytearray()
        for off in range(0, nbytes, 4):
            n = min(4, nbytes - off)
            save = {r: self.read_mem32(r) for r in
                    (self._SPI_USER, self._SPI_USER1, self._SPI_USER2,
                     self._SPI_MISO_DLEN, self._SPI_ADDR)}
            try:
                self.write_mem32(self._SPI_USER,
                                 self._SPI_USR_COMMAND | self._SPI_USR_MISO
                                 | self._SPI_USR_ADDR | self._SPI_USR_DUMMY)
                self.write_mem32(self._SPI_USER1,
                                 (23 << 26) | 7)            # 24 addr bits, 8 dummies
                self.write_mem32(self._SPI_USER2, (7 << 28) | 0x5A)
                self.write_mem32(self._SPI_ADDR, addr + off)   # right-aligned (24-bit)
                self.write_mem32(self._SPI_MISO_DLEN, n * 8 - 1)
                self.write_mem32(self._SPI_CMD, self._SPI_USR)
                for _ in range(1000):
                    if not (self.read_mem32(self._SPI_CMD) & self._SPI_USR):
                        break
                else:
                    raise RuntimeError("flash_sfdp: transaction never completed")
                out += self.read_mem32(self._SPI_W0).to_bytes(4, "little")[:n]
            finally:
                for r, v in save.items():
                    self.write_mem32(r, v)
        return bytes(out)

    def _spi1_user_read(self, cmd, nbytes):
        """Execute a bare SPI flash command on SPI1 via direct register access
        (SBA) and read back `nbytes` (<=4): command phase only + MISO phase —
        no stub, no ROM call, no serial. Touched registers are saved/restored.
        Flash must be idle (run after the gate); hart halted not required but
        we only call this from halted contexts."""
        save = {r: self.read_mem32(r) for r in
                (self._SPI_USER, self._SPI_USER2, self._SPI_MISO_DLEN)}
        try:
            self.write_mem32(self._SPI_USER, self._SPI_USR_COMMAND | self._SPI_USR_MISO)
            self.write_mem32(self._SPI_USER2, (7 << 28) | (cmd & 0xFF))
            self.write_mem32(self._SPI_MISO_DLEN, nbytes * 8 - 1)
            self.write_mem32(self._SPI_CMD, self._SPI_USR)
            for _ in range(1000):            # poll usr-clear: a cmd+4B read is ~µs
                if not (self.read_mem32(self._SPI_CMD) & self._SPI_USR):
                    break
            else:
                raise RuntimeError(f"_spi1_user_read: cmd 0x{cmd:02x} never completed")
            return self.read_mem32(self._SPI_W0) & ((1 << (8 * nbytes)) - 1)
        finally:
            for r, v in save.items():
                self.write_mem32(r, v)

    def flash_info(self):
        """Identify the flash die over JTAG alone: JEDEC RDID (cmd 0x9F) on SPI1.
        Returns dict(rdid, mfg, device, size_mb, vendor). Hart halted; runs the
        gate ladder first so SPI1 is attached/idle. Capacity is the JEDEC
        convention 2^cap bytes (e.g. 0x16 -> 4 MB; bytes arrive mfg, type, cap)."""
        self._ensure_flash_ready(lambda m: None, "flash_info")
        raw = self._spi1_user_read(0x9F, 3)
        mfg = raw & 0xFF                         # byte0 = manufacturer
        dev = ((raw >> 8) & 0xFF) << 8 | ((raw >> 16) & 0xFF)   # type:cap, flash-id style
        cap = (raw >> 16) & 0xFF
        size_mb = (1 << cap) // (1024 * 1024) if 0x10 <= cap <= 0x20 else None
        return dict(rdid=raw & 0xFFFFFF, mfg=mfg, device=dev, size_mb=size_mb,
                    vendor=self._FLASH_VENDORS.get(mfg, f"unknown(0x{mfg:02x})"))

    def flash_read_rom(self, addr, nwords):
        """Read nwords from RAW flash byte-offset `addr` via the ROM
        esp_rom_spiflash_read (needs flash_init done / the gate passing). Unlike
        flash_read_xip this is a TRUE raw flash offset (XIP maps the app's mapping,
        not raw flash), so it's the correct read for verify + inspection. Hart
        halted; icache toggled. Reuses the scratch staging buffer."""
        c = self._chip()
        buf = c["sram"]["data"]
        has_cache = "cache_disable_icache" in c.get("rom", {})
        if has_cache:                                   # newer chips (C5) lack the
            self.call_rom("cache_disable_icache")       # simple icache toggle; when
        self.call_rom("spiflash_read", args=(addr, buf, nwords * 4))
        words = self.read_mem(buf, nwords)
        if has_cache:                                   # halted there's no prefetch
            self.call_rom("cache_enable_icache")        # racing the ROM read anyway
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
        # clk first when tabled — empirically REQUIRED on the C5 when the app was
        # halted mid-flash-IO: attach+config_param alone left the gate failing in
        # 4/5 repro hits; config_clk(div=1) + attach + param recovered every one
        # (#33, scripts/c5_rom_read_repro.py bisect).
        if "spiflash_config_clk" in rom:
            self.call_rom("spiflash_config_clk", args=(1, 1))
        # attach — sets the legacy funcs' dummy cycles/read mode (without it the
        # ROM read returns garbage on a running app). ishspi=0 (default GPIO), legacy=0.
        if "spi_flash_attach" in rom:
            self.call_rom("spi_flash_attach", args=(0, 0))
        r, _ = self.call_rom("spiflash_config_param",
                             args=(0, chip_size, 0x10000, 0x1000, 0x100, 0xFFFF))
        return r

    def _ensure_flash_ready(self, _log, who, attempts=3):
        """Gate + recovery ladder: re-run flash_init (clk + attach + config_param)
        up to `attempts-1` times. Bench-measured on a C5 with a live flash-IO app
        (#33, c5_rom_read_repro 30-cycle soak): 18/18 gate failures recovered, 14
        on the first flash_init and 4 needing a second — one retry is not enough.
        Returns the final (ready, rom_words, magic)."""
        ready, rw, xw = self._rom_flash_ready()
        can_init = "spiflash_config_param" in self._chip().get("rom", {})
        for attempt in range(attempts - 1):
            if ready or not can_init:
                break
            _log(f"  {who}: gate failed — flash_init recovery "
                 f"(attempt {attempt + 1}/{attempts - 1})")
            self.flash_init()
            ready, rw, xw = self._rom_flash_ready()
        return ready, rw, xw

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
        has_cache = "cache_disable_icache" in rom
        try:
            if has_cache:
                self.call_rom("cache_disable_icache")
            ret, halted = self.call_rom("spiflash_read", args=(0x0, dest, nwords * 4))
            rom_words = self.read_mem(dest, nwords)
            if has_cache:
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
        ready, rw, xw = self._ensure_flash_ready(_log, "flash_write")
        if not ready:
            raise RuntimeError(
                "flash_write: ROM spiflash read-back self-test FAILED — the legacy "
                "ROM flash read path is not configured on this target (ROM read "
                f"{rw}, first-byte magic {xw}, want 0xE9). Refusing to erase/program "
                "(brick risk). flash_init (clk+attach+config_param) was retried; if "
                "the magic is still wrong this chip/revision needs an ECO workaround "
                "(see #33), or flash it with esptool.")
        c = self._chip()
        sram = c["sram"]
        buf = sram["data"]
        words = [int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)]
        _log(f"  flash_write: 0x{addr:08x} +{len(data)}B ({len(words)} words)")
        # unlock once
        self.call_rom("spiflash_unlock")
        # erase covering sectors — use 64 KiB BLOCK erase for fully-covered aligned
        # blocks: measured 4.4-5.7x faster than 16 sector erases on every die on
        # the bench (Winbond 987->211 ms, Puya 741->130, 0x46 391->88;
        # tmp/block_erase_bench.log). INVARIANT: a block is erased ONLY when the
        # write range covers all 16 of its sectors — partially-covered ends fall
        # back to sector erase (never erase what we won't rewrite; the pyOCD
        # fast_program bug class, scripts/incremental_invariant_test.py).
        first = addr & ~0xFFF
        last = (addr + len(data) - 1) & ~0xFFF
        rom = self._chip().get("rom", {})
        sa = first
        while sa <= last:
            if ("spiflash_erase_block" in rom and sa % 0x10000 == 0
                    and sa + 0x10000 - 0x1000 <= last):
                r, _ = self.call_rom("spiflash_erase_block", args=(sa >> 16,))
                if r != 0:
                    raise RuntimeError(f"flash erase block {sa >> 16} -> {r}")
                sa += 0x10000
            else:
                r, _ = self.call_rom("spiflash_erase_sector", args=(sa >> 12,))
                if r != 0:
                    raise RuntimeError(f"flash erase sector {sa >> 12} -> {r}")
                sa += 0x1000
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
            # read back in scratch-sized chunks — a single flash_read_rom of the
            # whole image overflows the scratch window for anything > ~4 KiB
            # (it clobbered the trap word and wedged the session; found when
            # 64 KiB verify=True was first exercised, 2026-06-12).
            for w0 in range(0, len(words), chunk_words):
                want = words[w0:w0 + chunk_words]
                got = self.flash_read_rom(addr + w0 * 4, len(want))
                if got != want:
                    raise RuntimeError(
                        f"flash_write: verify mismatch @0x{addr + w0 * 4:08x}")
            _log("  flash_write: verify OK")
        return True

    # === incremental flash — on-chip CRC-32 diff + write-only-changed + verify (#34) ===
    # The digest is a REAL CRC-32 (poly 0xEDB88320) computed ON-CHIP via the ROM
    # esp_rom_crc32_le over a scratch-staged sector read — only the 4-byte digest crosses
    # JTAG, never the sector data. This is the digest pyOCD/J-Flash and everyone who does
    # incremental RIGHT uses; STM32CubeProgrammer's 32-bit ADDITIVE sum (proven on silicon
    # to silently drop sum-preserving sector changes — docs/CUBEPROGRAMMER-BUGS.md) is the
    # anti-pattern we deliberately avoid.
    _CRC_INIT = 0xFFFFFFFF        # init passed to ROM crc32_le; host digest calibrated to match

    def _crc_host(self):
        """Return host_crc(bytes)->u32 reproducing the on-chip ROM crc32_le(_CRC_INIT, ·),
        CALIBRATED against the live ROM. The host and on-chip digest MUST agree bit-for-bit
        (the #1 incremental correctness trap, §9) — so we MEASURE the ROM's CRC convention
        from a known pattern rather than assume it. Cached per object."""
        if getattr(self, "_crc_host_fn", None):
            return self._crc_host_fn
        if "crc32_le" not in self._chip().get("rom", {}):
            raise RuntimeError("_crc_host: no crc32_le ROM symbol tabled for this chip")
        buf = self._chip()["sram"]["data"]
        pat = bytes((i * 73 + 19) & 0xFF for i in range(256))
        saved = self.read_mem(buf, 64)
        self.write_mem(buf, [int.from_bytes(pat[i:i + 4], "little") for i in range(0, 256, 4)])
        onchip, _ = self.call_rom("crc32_le", args=(self._CRC_INIT, buf, 256))
        self.write_mem(buf, saved)                              # leave scratch byte-identical
        onchip &= 0xFFFFFFFF
        cands = {
            "zlib^": lambda d: zlib.crc32(d) ^ 0xFFFFFFFF,
            "zlib": lambda d: zlib.crc32(d) & 0xFFFFFFFF,
            "raw(init)": lambda d: _crc32_le_raw(d, self._CRC_INIT),
            "raw(0)": lambda d: _crc32_le_raw(d, 0),
            "raw(0)^": lambda d: _crc32_le_raw(d, 0) ^ 0xFFFFFFFF,
        }
        for name, fn in cands.items():
            if fn(pat) == onchip:
                self._crc_host_fn, self._crc_host_name = fn, name
                return fn
        raise RuntimeError(f"_crc_host: no host candidate matched ROM crc32_le "
                           f"(onchip=0x{onchip:08x}) — convention unknown, refusing to diff")

    def flash_crc_region(self, addr, size):
        """On-chip CRC-32 of `size` bytes of RAW flash at byte-offset `addr`: ROM-read the
        region into scratch (stays on-chip) then ROM crc32_le over scratch — only the
        4-byte digest crosses JTAG. `size` must fit the scratch headroom (a 4 KiB sector
        does). Matches _crc_host() (same ROM, same _CRC_INIT). Hart halted; the ROM flash
        read path must be ready (flash_init / the gate, as flash_read_rom)."""
        return self._flash_crc_many([(addr, size)])[0]

    def _flash_crc_many(self, regions):
        """On-chip CRC-32 of several `(addr, size)` flash regions in one pass.
        The icache toggle is hoisted around the WHOLE loop (it exists to keep the
        cache off the SPI1 bus during ROM spiflash_read; crc32_le runs from ROM
        over SRAM scratch and doesn't care) — per-sector it was 4 ROM calls at
        ~13 ms of DMI round-trips each, 69% of flash_incremental's wall clock."""
        rom = self._chip().get("rom", {})
        buf = self._chip()["sram"]["data"]
        has_cache = "cache_disable_icache" in rom
        out = []
        if has_cache:
            self.call_rom("cache_disable_icache")
        try:
            for addr, size in regions:
                self.call_rom("spiflash_read", args=(addr, buf, size))
                crc, _ = self.call_rom("crc32_le", args=(self._CRC_INIT, buf, size))
                out.append(crc & 0xFFFFFFFF)
        finally:
            if has_cache:
                self.call_rom("cache_enable_icache")
        return out

    def flash_incremental(self, addr, data, log=None, verify=True, erase="always"):
        """Incrementally program `data` (bytes) to flash byte-offset `addr`: CRC each 4 KiB
        sector ON-CHIP, erase+program ONLY the sectors whose CRC differs from the image,
        then verify-after-write by re-CRC. Same brick-safety gate + ROM path as flash_write.
        `addr` 4 KiB-aligned; `data` 0xFF-padded to a sector multiple. An all-identical
        image writes nothing (the root early-out falls out of the loop).

        erase="auto": read each changed sector back and SKIP the erase when every bit
        transition is 1->0 (NOR programming can only clear bits), programming only the
        differing word-run in place. Big win for append-like / bit-clearing deltas
        (logs, counters, NVS-style); a net loss (~+50% per sector) on random changes
        because the read-back is paid and the erase happens anyway — hence opt-in.
        The verify re-CRC covers both paths identically.

        Returns dict(sectors, changed, written, overwritten, verified). Hart halted."""
        def _log(m):
            if log:
                log(m)
        if addr % 0x1000:
            raise ValueError("flash_incremental: addr must be 4 KiB-aligned")
        if len(data) % 0x1000:
            data = data + b"\xFF" * (0x1000 - len(data) % 0x1000)
        ready, rw, xw = self._ensure_flash_ready(_log, "flash_incremental")
        if not ready:
            raise RuntimeError("flash_incremental: ROM flash read self-test FAILED — "
                               "refusing to erase/program (brick risk)")
        host = self._crc_host()
        buf = self._chip()["sram"]["data"]
        nsec = len(data) // 0x1000
        crcs = self._flash_crc_many([(addr + s * 0x1000, 0x1000) for s in range(nsec)])
        changed = [s for s in range(nsec)
                   if crcs[s] != host(data[s * 0x1000:(s + 1) * 0x1000])]
        _log(f"  flash_incremental: {nsec} sectors, {len(changed)} differ "
             f"(digest {self._crc_host_name}) -> programming")
        # INVARIANT: every byte of every erased unit must be re-written. Today diff =
        # erase = write = 4 KiB so this holds by construction; if the erase unit ever
        # grows (e.g. 64 KiB spiflash_erase_block for speed), the whole block must be
        # programmed, not just the changed sectors — skipping "unchanged" data inside
        # an erased unit is pyOCD's fast_program data-loss bug
        # (docs/PYOCD-INCREMENTAL-PROOF.md §3; scripts/incremental_invariant_test.py).
        overwritten = 0
        for s in changed:
            sa = addr + s * 0x1000
            img = data[s * 0x1000:(s + 1) * 0x1000]
            if erase == "auto":
                # NOR programming only clears bits (1->0). Read the sector back:
                # if no bit needs 0->1, skip the erase and program ONLY the
                # differing word-run. Wins on append/bit-clearing deltas; the
                # read-back makes it a net LOSS on random changes — opt-in.
                cur = b"".join(w.to_bytes(4, "little")
                               for w in self.flash_read_rom(sa, 0x400))
                if all(c & n == n for c, n in zip(cur, img)):
                    w0 = next(i for i in range(0, 0x1000, 4)
                              if cur[i:i + 4] != img[i:i + 4])
                    w1 = next(i for i in range(0xFFC, -4, -4)
                              if cur[i:i + 4] != img[i:i + 4]) + 4
                    self.call_rom("spiflash_unlock")
                    self.write_mem(buf, [int.from_bytes(img[i:i + 4], "little")
                                         for i in range(w0, w1, 4)])
                    r, _ = self.call_rom("spiflash_write", args=(sa + w0, buf, w1 - w0))
                    if r:
                        raise RuntimeError(
                            f"flash_incremental: overwrite @0x{sa + w0:08x} -> {r}")
                    overwritten += 1
                    _log(f"  flash_incremental: sector {s} overwritten in place "
                         f"({w1 - w0} bytes, no erase)")
                    continue
            self.call_rom("spiflash_unlock")
            r, _ = self.call_rom("spiflash_erase_sector", args=(sa >> 12,))
            if r:
                raise RuntimeError(f"flash_incremental: erase sector {sa >> 12} -> {r}")
            self.write_mem(buf, [int.from_bytes(img[i:i + 4], "little")
                                 for i in range(0, 0x1000, 4)])
            r, _ = self.call_rom("spiflash_write", args=(sa, buf, 0x1000))
            if r:
                raise RuntimeError(f"flash_incremental: write @0x{sa:08x} -> {r}")
        verified = 0
        if verify and changed:
            vcrcs = self._flash_crc_many([(addr + s * 0x1000, 0x1000) for s in changed])
            for s, crc in zip(changed, vcrcs):
                if crc != host(data[s * 0x1000:(s + 1) * 0x1000]):
                    raise RuntimeError(
                        f"flash_incremental: verify FAILED @0x{addr + s * 0x1000:08x}")
                verified += 1
        _log(f"  flash_incremental: programmed {len(changed)}/{nsec} "
             f"({overwritten} in place), verified {verified}")
        return dict(sectors=nsec, changed=len(changed), written=len(changed),
                    overwritten=overwritten, verified=verified)

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

