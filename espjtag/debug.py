"""espjtag.debug — the full RISC-V debugger built on the transport: halt/resume,
GPR/CSR read+write (abstract command), memory read/write (System Bus Access),
diag, and the reset_run() method. EspUsbJtag is the public class most users want.
"""

import time

import usb.util

from .constants import (
    DMCONTROL, DMSTATUS, HARTINFO, ABSTRACTCS, COMMAND, DATA0,
    SBCS, SBADDRESS0, SBDATA0,
    DM_DMACTIVE, DM_NDMRESET, DM_ACKHAVERESET, DM_RESUMEREQ, DM_HALTREQ,
    DM_ALLHALTED, DM_ALLRUNNING, DM_ALLHAVERESET,
    ABS_BUSY, ABS_CMDERR,
    CMD_ACCESS_REGISTER, AC_TRANSFER, AC_WRITE, AC_AARSIZE32,
    CSR_DPC, REG_GPR_BASE,
)
from .transport import EspUsbJtagTransport


class EspUsbJtag(EspUsbJtagTransport):
    # === RISC-V Debug Module: halt / resume / examine =====================
    def dm_read(self, addr):
        d, _ = self.dmi_read(addr)
        return d

    def halt(self, timeout=200):
        """Request halt and wait for allhalted. Returns True if halted."""
        self.dmi_write(DMCONTROL, DM_HALTREQ | DM_DMACTIVE)
        for _ in range(timeout):
            if self.dm_read(DMSTATUS) & DM_ALLHALTED:
                self.dmi_write(DMCONTROL, DM_DMACTIVE)     # drop haltreq
                return True
        return False

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

    def read_mem(self, addr, nwords):
        """Read nwords 32-bit words starting at addr (autoincrement)."""
        self._sb_setup(autoincrement=True, readondata=True, readonaddr=True)
        self.dmi_write(SBADDRESS0, addr & 0xFFFFFFFF)
        out = [self.dm_read(SBDATA0) for _ in range(nwords)]
        self.dmi_write(SBCS, 0)
        return out

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


def diag(usb_path=None, log=print):
    """Read-only RISC-V Debug Module dump (IDCODE + dmcontrol/dmstatus/hartinfo)
    over the built-in USB-JTAG — no reset, no side effects on the running app."""
    return EspUsbJtag(usb_path).diag(log=log)


def selftest(usb_path=None, rounds=3):
    """Verify the JTAG stack against a live C6: IDCODE, DTMCS, and a dmstatus DMI
    read must read their known values, deterministically, `rounds` times. Returns
    (passed, total). Read-only — safe to run against a board running the app."""
    C6_IDCODE = 0x0000DC25
    DMSTATUS_C6 = 0x00030CA2
    passed = 0
    for r in range(rounds):
        j = EspUsbJtag(usb_path)
        ic = j.read_idcode()
        dt = j.read_dtmcs()
        ds, st = j.dmi_read(DMSTATUS)
        ok = (ic == C6_IDCODE and (dt & 0xF) == 1 and j.abits == 7
              and ds == DMSTATUS_C6 and st == 0)
        passed += ok
        usb.util.dispose_resources(j.dev)
        print(f"  round {r}: IDCODE=0x{ic:08x} DTMCS=0x{dt:08x} "
              f"dmstatus=0x{ds:08x}(st{st})  {'PASS' if ok else 'FAIL'}")
    print(f"  selftest: {passed}/{rounds} passed")
    return passed, rounds

