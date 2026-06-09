"""espjtag.transport — the esp_usb_jtag USB transport + JTAG TAP state machine +
DMI read/write. This is the BASE layer: everything (the reset fix and the full
debugger) builds on it. It has NO debug-module knowledge beyond DMI access, so a
minimal consumer (e.g. an esptool reboot fix) can depend on just this.

Ported from openocd-esp32 esp_usb_jtag.c + bitq.c.
"""

import time

import usb.core
import usb.util

from .constants import (
    VID, PID, VENDOR_CLASS, CMD_FLUSH, _clk, _bits_to_int,
    IR_DTMCS, IR_DMI, IR_LEN, DMI_NOP, DMI_READ, DMI_WRITE,
)


class EspUsbJtagTransport:
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

