"""esp_usb_jtag.py — pure-Python (pyusb) RISC-V JTAG debug client for the
ESP32-C3/C5/C6/H2 built-in USB-JTAG. A from-scratch reimplementation of the parts
of OpenOCD needed to talk to the RISC-V Debug Module — no OpenOCD binary.

WORKS (deterministic, verified against a live C6, mirrors OpenOCD):
  - esp_usb_jtag USB transport (vendor iface 0xFF, bulk IN/OUT, SETDIV clock)
  - a tracked JTAG TAP state machine (like bitq.c) — scans are correct from any
    state; the IN endpoint is drained between ops to stay in sync
  - read IDCODE (0x0000dc25), DTMCS (abits/idle), and DMI read/write
    (dmstatus 0x00030ca2, dmcontrol, hartinfo) — see `python -m` / diag()
  - reset_run(): ndmreset + halt/resume — reboots a RUNNING core cleanly

NOT YET: booting a chip from the post-flash ROM download state. OpenOCD's
`reset run` does a deeper halt/examine/resume (dcsr setup) to leave ROM; we don't
replicate that yet, so flash.py still uses OpenOCD for the post-flash C6 boot.
See docs/C6-USJ-RESET.md.

Protocol ported from openocd-esp32 src/jtag/drivers/esp_usb_jtag.c + bitq.c, and
the RISC-V external-debug spec (debug_defines.h):
  - OUT = 4-bit command nibbles, two per byte (high first):
        CMD_CLK   0b0<cap><tms><tdi>   one TCK; cap=1 captures TDO to the IN ep
        CMD_FLUSH 0b1010                pad + flush the IN ep
  - VEND_JTAG_SETDIV (req 0, bmRequestType 0x40) sets TCK speed
  - IR(5b): IDCODE=0x01 DTMCS=0x10 DMI=0x11; dmi scan = addr<<34 | data<<2 | op
"""

import struct
import time

import usb.core
import usb.util

VID, PID = 0x303A, 0x1001
VENDOR_CLASS = 0xFF                        # the JTAG interface is vendor-spec;
                                           # iface 0/1 are the CDC console — NOT it
CAPS_DESCRIPTOR = 0x2000                   # GET_DESCRIPTOR wValue

# 4-bit OUT command nibbles (esp_usb_jtag.c)
def _clk(tms, tdi, cap=0):
    return (cap << 2) | (tms << 1) | tdi   # 0b0 cap tms tdi
CMD_FLUSH = 0xA


def _bits_to_int(bits):
    return sum(b << i for i, b in enumerate(bits))

# JTAG IR opcodes (5-bit on these RISC-V cores). NOTE: this client is RISC-V only
# (C3/C5/C6/H2). The Xtensa parts (S2/S3) use the SAME esp_usb_jtag USB transport
# but a DIFFERENT debug module (Xtensa OCD/TRAX, not the RISC-V Debug Module) — no
# DMI/dmcontrol/ndmreset — so reset_run() here does not apply to them (and the S3
# boots fine via esptool anyway).
IR_DTMCS = 0x10
IR_DMI = 0x11
IR_LEN = 5
# DMI op field: 0=nop, 1=read, 2=write. dmi scan = (address<<34)|(data<<2)|op
DMI_NOP = 0
DMI_READ = 1
DMI_WRITE = 2
# RISC-V Debug Module register addresses (DMI address space) — debug_defines.h
DMCONTROL = 0x10
DMSTATUS = 0x11
HARTINFO = 0x12
ABSTRACTCS = 0x16     # abstract command status (busy, cmderr)
COMMAND = 0x17        # abstract command (access register etc.)
ABSTRACTAUTO = 0x18
DATA0 = 0x04          # abstract data 0..N
PROGBUF0 = 0x20       # program buffer 0..N
# System Bus Access registers (memory r/w without a running hart)
SBCS = 0x38
SBADDRESS0 = 0x39
SBDATA0 = 0x3C
# dmcontrol bits
DM_DMACTIVE = 1 << 0
DM_NDMRESET = 1 << 1
DM_ACKHAVERESET = 1 << 28          # 0x10000000
DM_HARTRESET = 1 << 29             # 0x20000000
DM_RESUMEREQ = 1 << 30            # 0x40000000
DM_HALTREQ = 1 << 31             # 0x80000000
# dmstatus bits
DM_ALLHALTED = 1 << 9
DM_ALLRUNNING = 1 << 11
DM_ALLHAVERESET = 1 << 19
# abstractcs
ABS_BUSY = 1 << 12
ABS_CMDERR = 0x7 << 8
# command: access register
CMD_ACCESS_REGISTER = 0 << 24
AC_TRANSFER = 1 << 17
AC_WRITE = 1 << 16
AC_AARSIZE32 = 2 << 20
AC_POSTEXEC = 1 << 18
# CSR numbers
CSR_DCSR = 0x7B0
CSR_DPC = 0x7B1
# GPR register index in abstract-command regno space (0x1000 + gpr)
REG_GPR_BASE = 0x1000


class EspUsbJtag:
    def __init__(self, usb_path=None):
        # Match the right unit when several 303a:1001 are on the bus, by the
        # sysfs port chain (e.g. "1-1.3.1.3.1" -> bus 1, ports (1,3,1,3,1)).
        def _match(d):
            if usb_path is None:
                return True
            try:
                bus_s, ports_s = usb_path.split("-", 1)
                want_bus = int(bus_s)
                want_ports = tuple(int(p) for p in ports_s.split("."))
            except ValueError:
                return True
            return d.bus == want_bus and tuple(d.port_numbers or ()) == want_ports

        dev = next((d for d in usb.core.find(find_all=True, idVendor=VID,
                                             idProduct=PID) if _match(d)), None)
        if dev is None:
            raise RuntimeError(f"esp_usb_jtag: no 303a:1001 matching {usb_path!r}")
        self.dev = dev
        cfg = dev.get_active_configuration()
        # The JTAG interface is the VENDOR-SPEC one (0xFF). iface 0/1 are the
        # CDC-ACM console (classes 0x02/0x0a) — writing JTAG to those just times
        # out (the bug). Find the 0xFF interface dynamically.
        intf = usb.util.find_descriptor(
            cfg, bInterfaceClass=VENDOR_CLASS)
        if intf is None:
            raise RuntimeError("esp_usb_jtag: no vendor-spec (JTAG) interface")
        self.iface = intf.bInterfaceNumber
        try:
            if dev.is_kernel_driver_active(self.iface):
                dev.detach_kernel_driver(self.iface)
        except (NotImplementedError, usb.core.USBError):
            pass
        usb.util.claim_interface(dev, self.iface)
        self.ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(
                e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(
                e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        if not self.ep_out or not self.ep_in:
            raise RuntimeError("esp_usb_jtag: bulk endpoints not found")
        # A conservative TCK divisor — speed is irrelevant for a few reset writes.
        self.dev.ctrl_transfer(0x40, 0, 20, 0, None)   # VEND_JTAG_SETDIV
        self._nibbles = []
        self.state = self.RESET            # TAP state unknown; reset_tap syncs it

    # --- low-level nibble stream ------------------------------------------
    def _emit(self, nib):
        self._nibbles.append(nib & 0xF)

    def _drain_in(self):
        """Discard any stale bytes sitting in the IN endpoint. Without this, a
        previous scan's residual/padding bytes desync the NEXT read (the bug where
        only the first access per session worked and the rest read 0/stale)."""
        try:
            while True:
                self.ep_in.read(self.ep_in.wMaxPacketSize or 64, timeout=20)
        except usb.core.USBError:
            pass                            # timeout = endpoint empty

    def _send(self):
        """Append CMD_FLUSH, pad to a whole byte, and write the queued OUT nibble
        stream. Does NOT read IN — call _recv() for captured TDO."""
        self._emit(CMD_FLUSH)
        if len(self._nibbles) % 2:
            self._emit(CMD_FLUSH)                       # can't send a half-byte
        buf = bytes((self._nibbles[i] << 4) | self._nibbles[i + 1]
                    for i in range(0, len(self._nibbles), 2))
        self._nibbles = []
        self.ep_out.write(buf)

    def _recv(self, want_tdo_bits):
        """Read want_tdo_bits captured TDO bits from the IN endpoint (LSB-first
        per byte, in capture order). Read whole max-packet chunks (a short read
        triggers libusb Overflow)."""
        if not want_tdo_bits:
            return []
        mps = self.ep_in.wMaxPacketSize or 64
        need = (want_tdo_bits + 7) // 8
        rdlen = ((need + mps - 1) // mps) * mps
        data = self.ep_in.read(rdlen, timeout=1000)
        bits = []
        for byte in data:
            for b in range(8):
                bits.append((byte >> b) & 1)
        return bits[:want_tdo_bits]

    def _flush(self, want_tdo_bits=0):
        """Send the queued OUT stream and read back want_tdo_bits (legacy combined
        helper — used where a single op queues + captures in one go)."""
        self._send()
        return self._recv(want_tdo_bits)

    # === JTAG TAP state machine ===========================================
    # Explicit state tracking (mirrors OpenOCD bitq.c): every clock updates
    # self.state, and _goto() emits the exact TMS path between any two states.
    # This is what makes scans deterministic regardless of where we start.
    #
    # State graph: (state) -> (next on TMS=0, next on TMS=1)
    RESET, IDLE = "reset", "idle"
    SEL_DR, CAP_DR, SHIFT_DR, EXIT1_DR, PAUSE_DR, EXIT2_DR, UPDATE_DR = (
        "seldr", "capdr", "shiftdr", "e1dr", "pausedr", "e2dr", "updr")
    SEL_IR, CAP_IR, SHIFT_IR, EXIT1_IR, PAUSE_IR, EXIT2_IR, UPDATE_IR = (
        "selir", "capir", "shiftir", "e1ir", "pauseir", "e2ir", "upir")
    _NEXT = {
        RESET:    (IDLE,     RESET),
        IDLE:     (IDLE,     SEL_DR),
        SEL_DR:   (CAP_DR,   SEL_IR),
        CAP_DR:   (SHIFT_DR, EXIT1_DR),
        SHIFT_DR: (SHIFT_DR, EXIT1_DR),
        EXIT1_DR: (PAUSE_DR, UPDATE_DR),
        PAUSE_DR: (PAUSE_DR, EXIT2_DR),
        EXIT2_DR: (SHIFT_DR, UPDATE_DR),
        UPDATE_DR:(IDLE,     SEL_DR),
        SEL_IR:   (CAP_IR,   RESET),
        CAP_IR:   (SHIFT_IR, EXIT1_IR),
        SHIFT_IR: (SHIFT_IR, EXIT1_IR),
        EXIT1_IR: (PAUSE_IR, UPDATE_IR),
        PAUSE_IR: (PAUSE_IR, EXIT2_IR),
        EXIT2_IR: (SHIFT_IR, UPDATE_IR),
        UPDATE_IR:(IDLE,     SEL_DR),
    }

    def _clock_tms(self, tms, tdi=0, cap=0):
        """Emit one TCK with the given TMS, and advance the tracked TAP state."""
        self._emit(_clk(tms, tdi, cap))
        self.state = self._NEXT[self.state][tms]

    def _goto(self, target):
        """Walk to `target` by emitting TMS bits (TDI=0, no capture). Breadth-first
        shortest path through the state graph — always correct from any state."""
        if self.state == target:
            return
        # BFS for the shortest TMS sequence.
        from collections import deque
        q = deque([(self.state, [])])
        seen = {self.state}
        while q:
            st, path = q.popleft()
            if st == target:
                for tms in path:
                    self._clock_tms(tms)
                return
            for tms in (0, 1):
                nxt = self._NEXT[st][tms]
                if nxt not in seen:
                    seen.add(nxt)
                    q.append((nxt, path + [tms]))
        raise RuntimeError(f"no TMS path {self.state}->{target}")

    def reset_tap(self):
        # 5x TMS=1 forces Test-Logic-Reset from ANY state, then to Idle.
        for _ in range(5):
            self._clock_tms(1)
        self.state = self.RESET
        self._goto(self.IDLE)

    def _scan(self, value, nbits, ir=False, capture=False):
        """Shift nbits LSB-first through IR or DR, capturing TDO if requested.
        Leaves the TAP in Update->Idle. Mirrors bitq_scan_field: N-1 bits in
        Shift with TMS=0, the last bit with TMS=1 (Exit1) — all captured."""
        self._goto(self.SHIFT_IR if ir else self.SHIFT_DR)
        for i in range(nbits):
            last = i == nbits - 1
            self._clock_tms(1 if last else 0, (value >> i) & 1,
                            1 if capture else 0)   # last bit TMS=1 -> Exit1
        # now in Exit1; return to Idle for the next op.
        self._goto(self.IDLE)

    def _idle(self, n):
        """Clock n cycles in Run-Test-Idle (DMI needs `idle` cycles to settle)."""
        self._goto(self.IDLE)
        for _ in range(n):
            self._clock_tms(0)

    def read_idcode(self):
        """Reset TAP (auto-loads IDCODE), scan DR out."""
        self.reset_tap()
        self._drain_in()
        self._scan(0, 32, capture=True)
        self._send()
        return _bits_to_int(self._recv(32))

    # --- DTMCS (IR=0x10): version, abits (DMI addr width), idle cycles ----
    def read_dtmcs(self):
        self._drain_in()                             # clear stale IN bytes first
        self.reset_tap()
        self._scan(IR_DTMCS, IR_LEN, ir=True)        # select DTMCS
        self._scan(0, 32, capture=True)              # read DR
        self._send()
        v = _bits_to_int(self._recv(32))
        self.dtmcs = v
        self.abits = (v >> 4) & 0x3F
        self.idle = (v >> 12) & 0x7
        return v

    def _ensure_dtmcs(self):
        if not hasattr(self, "abits") or not self.abits:
            self.read_dtmcs()

    # --- DMI: scan = address<<34 | data<<2 | op, width = abits+34 -------------
    def _dmi(self, address, data, op):
        self._ensure_dtmcs()
        width = self.abits + 34
        self._drain_in()                             # clear stale IN bytes first
        self.reset_tap()
        self._scan(IR_DMI, IR_LEN, ir=True)          # select DMI
        word = (address << 34) | ((data & 0xFFFFFFFF) << 2) | (op & 0x3)
        self._scan(word, width, capture=True)        # the DMI access
        self._idle(max(self.idle, 1))                # settle
        self._send()
        out = _bits_to_int(self._recv(width))
        return (out >> 2) & 0xFFFFFFFF, out & 0x3    # data, op-status

    def dmi_write(self, address, data):
        self._dmi(address, data, DMI_WRITE)

    def dmi_read(self, address, retries=8):
        """RISC-V DTM read: the data from a READ scan is the result of the PREVIOUS
        access, so you scan READ@addr then a NOP to collect addr's data — BOTH DR
        scans in ONE TAP session (one IR-select, no reset between) or the pending
        read is lost. op-status 3 = busy -> add idle + retry."""
        self._ensure_dtmcs()
        width = self.abits + 34
        for _ in range(retries):
            self._drain_in()
            self.reset_tap()
            self._scan(IR_DMI, IR_LEN, ir=True)              # select DMI once
            rd = (address << 34) | (DMI_READ & 0x3)
            self._scan(rd, width, capture=True)              # issue read
            self._idle(max(self.idle, 1))
            nop = (0 << 34) | (DMI_NOP & 0x3)
            self._scan(nop, width, capture=True)             # collect result
            self._idle(max(self.idle, 1))
            self._send()
            # Both scans' captures come back concatenated, in order: the first
            # `width` bits are the READ-phase, the next `width` are the NOP-phase
            # (= the data for `address`). Read all at once.
            allbits = self._recv(2 * width)
            out = _bits_to_int(allbits[width:2 * width])      # nop-phase
            data, status = (out >> 2) & 0xFFFFFFFF, out & 0x3
            if status == 0:
                return data, status
            if status == 3:
                self.idle += 1
        return data, status

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


def reset_run(usb_path=None, log=None) -> bool:
    """Pulse ndmreset on the matching C6 so it boots the app. Returns True on a
    clean run. Pure Python — no OpenOCD."""
    EspUsbJtag(usb_path).reset_run(log=log)
    return True


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

# CLI lives in espjtag/__main__.py (`python -m espjtag`).
