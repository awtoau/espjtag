"""xtensa_mock.py — a no-hardware MOCK of XtensaXDM (the OpenOCD `dummy` adapter /
probe-rs `FakeProbe` analogue). Backed by a dict "RAM", it RECORDS every memory +
register op the flasher issues and serves reads from the model — so the WHOLE
flasher op-sequence (load writes, the start/wait_algorithm register dance, mem-arg
buffers) is validated with ZERO JTAG.

It is NOT a core emulator: it does not execute the stub. `start_algorithm` records
the regs + resume; `wait_algorithm` returns a scripted result (`set_run_result`).
So: host-side correctness (what bytes/regs would be driven) is testable here; only
"does the silicon run the stub" needs a real S3 — exactly the line probe-rs/OpenOCD
draw (FakeProbe/dummy vs HIL).

Use it anywhere a real XtensaXDM goes:
    x = MockXtensaXDM()
    fl = XtensaFlasher(x, "esp32s3"); fl.load("cmd_test1"); fl.run()
    x.writes        # [(addr, [words]), ...] every write the load+run did
    x.mem[addr]     # the model memory
"""


class MockXtensaXDM:
    """Records ops, serves reads from a dict model. Same surface XtensaFlasher
    uses: write_mem / read_mem / start_algorithm / wait_algorithm (+ the reg/
    sr setters the unified algorithm port calls, so it can drive this too)."""

    def __init__(self, idcode=0x120034E5):
        self.mem = {}                     # word-addressed model: {addr: u32}
        self.regs = {}                    # a-regs + named SRs the flasher sets
        self.writes = []                  # [(addr, [words])] — every write, in order
        self.reads = []                   # [(addr, nwords)]
        self.ops = []                     # full op log (kind, *args) for assertions
        self.resumes = []                 # [(entry_point,)] each resume
        self._run_result = (0,)           # what wait_algorithm reports for a2
        self._chip_dict = {"name": "S3"}
        self.idcode = idcode

    # --- the XtensaXDM surface XtensaFlasher uses --------------------------
    def write_mem(self, addr, words):
        words = list(words)
        self.writes.append((addr, words))
        self.ops.append(("write_mem", addr, len(words)))
        for i, w in enumerate(words):
            self.mem[addr + 4 * i] = w & 0xFFFFFFFF

    def read_mem(self, addr, nwords):
        self.reads.append((addr, nwords))
        self.ops.append(("read_mem", addr, nwords))
        # serve from the model; default 0xA5A5A5A5 for never-written (so an
        # untouched mem-arg buffer is visibly "unwritten", like real 0xAA RAM).
        return [self.mem.get(addr + 4 * i, 0xA5A5A5A5) for i in range(nwords)]

    # --- the unified start/wait_algorithm port calls these on `x` ----------
    def _set_ar(self, n, val):
        self.regs[f"a{n}"] = val & 0xFFFFFFFF
        self.ops.append(("set_ar", n, val & 0xFFFFFFFF))

    def _get_ar(self, n):
        return self.regs.get(f"a{n}", 0)

    def _set_sr_a3(self, wsr_ins, val):
        self.regs[("sr", wsr_ins)] = val & 0xFFFFFFFF
        self.ops.append(("set_sr", wsr_ins, val & 0xFFFFFFFF))

    def _get_sr_a3(self, rsr_ins):
        return self.regs.get(("sr", rsr_ins), 0)

    def nar_write(self, naraddr, value):
        self.ops.append(("nar_write", naraddr, value))
        # INS_RFDO resume: record it; for the mock, "running" = land the scripted
        # result and mark STOPPED so wait_algorithm sees a halt.
        if naraddr == _DIR0EXEC and value == _INS_RFDO:
            self.resumes.append((self.regs.get(("sr", _EPC6_WSR)),))
            self.regs["a2"] = self._run_result[0] & 0xFFFFFFFF
            self._stopped = True

    def nar_read(self, naraddr):
        self.ops.append(("nar_read", naraddr))
        if naraddr == _DSR:
            return _OCDDSR_STOPPED if getattr(self, "_stopped", False) else 0
        return 0

    # start_algorithm / wait_algorithm are GENERIC over the primitives above
    # (_set_ar/_set_sr_a3/nar_write/nar_read), so we reuse the REAL ones verbatim
    # — the mock isn't a second implementation, just a different backend.
    from .xtensa import XtensaXDM as _RealXDM
    start_algorithm = _RealXDM.start_algorithm
    wait_algorithm = _RealXDM.wait_algorithm
    _REG_WSR = _RealXDM._REG_WSR
    _reg_set = _RealXDM._reg_set
    _reg_get = _RealXDM._reg_get
    del _RealXDM

    # --- test controls -----------------------------------------------------
    def set_run_result(self, a2):
        """Script what the next algorithm run 'returns' in a2."""
        self._run_result = (a2,)

    def powerup(self):
        self.ops.append(("powerup",))

    def halt(self, *a, **k):
        self.ops.append(("halt",))
        self._stopped = True
        return True

    def resume(self):
        self.ops.append(("resume",))


# constants the mock matches on (all live in espjtag.xtensa)
from .xtensa import (                              # noqa: E402
    DIR0EXEC as _DIR0EXEC, DSR as _DSR, OCDDSR_STOPPED as _OCDDSR_STOPPED,
    INS_RFDO as _INS_RFDO, INS_WSR_EPC6_A3 as _EPC6_WSR,
)
