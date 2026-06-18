"""mock.py — a no-hardware MOCK of the RISC-V transport (EspUsbJtag), the
companion to xtensa_mock.MockXtensaXDM. Records every DMI / Debug-Module
operation and serves reads from a register model — so RISC-V debug sequences
(reset_run, halt, register access) are validated for the RIGHT OPERATION
SEQUENCE with ZERO JTAG.

Like the Xtensa mock, it is NOT a core emulator: it models the Debug Module
registers as a dict and lets you script reads (`set_dm`). It validates "does
espjtag issue the right DMI writes?" (e.g. the golden OpenOCD reset_run
sequence) — not "does the silicon respond". OpenOCD's `dummy` adapter / probe-rs
`FakeProbe` analogue at the DMI altitude.

    j = MockEspUsbJtag()
    EspUsbJtag.reset_run(j)         # or via a subclass — see the test
    j.dmi_writes                    # [(address, value), ...] every DMI write
"""


class MockEspUsbJtag:
    """Records DMI ops, serves Debug-Module reads from a dict model. Implements
    the transport surface debug.py uses: dmi_write / dmi_read / dm_read /
    read_idcode / read_dtmcs. usb.util.dispose_resources(self.dev) is a no-op
    here (self.dev is a stub)."""

    def __init__(self, idcode=0x0000DC25):     # default: an ESP32-C6 IDCODE
        self.dm = {}                  # Debug-Module register model {address: u32}
        self.dm_seq = {}              # {address: [v0, v1, ...]} scripted read queue
        self.dmi_writes = []          # [(address, value)] every dmi_write, in order
        self.dmi_reads = []           # [address]
        self.ops = []                 # full op log
        self.idcode = idcode
        self.dev = _StubDev()         # so usb.util.dispose_resources(j.dev) works

    # --- the transport surface debug.py calls ------------------------------
    def dmi_write(self, address, data, retries=8):
        self.dmi_writes.append((address, data & 0xFFFFFFFF))
        self.ops.append(("dmi_write", address, data & 0xFFFFFFFF))
        self.dm[address] = data & 0xFFFFFFFF

    def dmi_read(self, address, retries=8):
        self.dmi_reads.append(address)
        self.ops.append(("dmi_read", address))
        # a scripted read sequence (e.g. ABSTRACTCS busy, busy, ready) takes
        # priority and yields one value per read; its last value sticks.
        seq = self.dm_seq.get(address)
        if seq:
            return (seq.pop(0) if len(seq) > 1 else seq[0]), 0
        return self.dm.get(address, 0), 0          # (data, op-status)

    def dm_read(self, address):
        return self.dmi_read(address)[0]

    def read_idcode(self):
        self.ops.append(("read_idcode",))
        return self.idcode

    def read_dtmcs(self):
        self.ops.append(("read_dtmcs",))
        return self.dm.get("dtmcs", 0)

    # --- test controls -----------------------------------------------------
    def set_dm(self, address, value):
        """Script a Debug-Module register read (e.g. DMSTATUS = allhalted)."""
        self.dm[address] = value & 0xFFFFFFFF

    def set_dm_sequence(self, address, values):
        """Script SUCCESSIVE reads of `address` (e.g. ABSTRACTCS = [busy, busy,
        ready]); one value is consumed per read, the last sticks. Used to drive
        retry/poll paths like _abstract_wait."""
        self.dm_seq[address] = [v & 0xFFFFFFFF for v in values]

    # The real debug ops are GENERIC over the transport surface above
    # (dmi_write / dm_read), so we reuse EspUsbJtag's verbatim — the mock is just a
    # different backend, exactly like MockXtensaXDM borrows XtensaXDM. This lets a
    # test call j.halt() / j.read_register() and assert the DMI sequence with no
    # hardware. (Methods that touch USB endpoints directly are NOT borrowed.)
    from .debug import EspUsbJtag as _RealDM
    halt = _RealDM.halt
    resume = _RealDM.resume
    examine = _RealDM.examine
    _abstract_wait = _RealDM._abstract_wait
    read_register = _RealDM.read_register
    write_register = _RealDM.write_register
    _wdt_disable = _RealDM._wdt_disable
    _chip = _RealDM._chip                  # chips.lookup(read_idcode()) -> chip dict
    _sb_setup = _RealDM._sb_setup
    _sbcs_value = staticmethod(_RealDM._sbcs_value)   # keep it static (see debug.py)
    read_mem32 = _RealDM.read_mem32
    write_mem32 = _RealDM.write_mem32
    del _RealDM


class _StubCtx:
    def dispose(self, device):
        pass


class _StubDev:
    """Stub so usb.util.dispose_resources(j.dev) is a harmless no-op in tests —
    it calls device._ctx.dispose(device), which we satisfy."""
    _ctx = _StubCtx()
