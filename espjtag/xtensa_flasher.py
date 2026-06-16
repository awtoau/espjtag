"""xtensa_flasher.py — S3 flash-over-JTAG by running OpenOCD's PREBUILT flasher
stub, ported VERBATIM from openocd-esp32 (#29). NOT the old hand-built bridge
(abandoned — couldn't do nested window-spill). This loads the SAME stub image
OpenOCD loads and runs it the SAME way.

Ported from (espressif/openocd-esp32 @ the upstream.lock SHA):
  src/target/espressif/esp_algorithm.c        — load_func_image / run_image
  src/target/espressif/esp_xtensa_algorithm.c — algo_init reg setup, tramp
  src/target/xtensa/xtensa.c                  — start/wait_algorithm
  src/flash/nor/esp32s3.c                     — the S3 flash driver wiring

The blobs + per-command configs are in xtensa_stubs.py (generated from the same
source). Mirrors the C structure exactly; comments cite the C function/line.
"""
import time

from .xtensa_stubs import STUBS, TRAMP_WIN


def _bytes_to_words(b):
    """Pack bytes -> list of LE uint32 (write_mem takes words). Pads to 4."""
    if len(b) % 4:
        b = b + b"\x00" * (4 - len(b) % 4)
    return [int.from_bytes(b[i:i + 4], "little") for i in range(0, len(b), 4)]

# ESP32-S3 has REVERSED memory: the instruction bus sees IRAM in reverse word
# order vs the data bus (esp_algorithm.c load_section_from_image comment). The
# whole stub lives in one IRAM area from iram_org; data goes to dram_org reversed.
S3_REVERSED = True

# esp_xtensa_algo_regs_init_start (esp_xtensa_algorithm.c:47): the run-start regs.
#   a0 = 0           (return addr — the tramp's frame)
#   a1 = sp          (stack_addr, 16-aligned, -16)
#   a8 = stub.entry  (the windowed entry the tramp callx8's into)
#   windowbase = 0, windowstart = 1
#   ps = 0x60025     (WOE + UM + debug INTLEVEL 6)
# args go in a2..a6 (a2 is IN_OUT = the return code).
RUN_PS = 0x60025
ARG_REGS = ("a2", "a3", "a4", "a5", "a6")    # ESP_XTENSA_STUB_ARGS_FUNC_START..

# command numbers (esp_stub.h enum) — the ones we use for flashing.
ESP_STUB_CMD_FLASH_WRITE_DEFLATED = "cmd_flash_write_deflated"


def _align_up(v, a):
    return (v + a - 1) & ~(a - 1)


def _align4(b):
    """pad bytes up to a 4-byte boundary (NUL pad) — normal (non-reversed) write."""
    if len(b) % 4:
        b = bytes(b) + b"\x00" * (4 - len(b) % 4)
    return bytes(b)


def _reverse_binary(src):
    """1:1 port of reverse_binary() (esp_algorithm.c:345). DO NOT change.
        size_t remaining = length % 4; offset = 0; aligned_len = ALIGN_UP(length,4)
        if remaining: memset(dest+remaining, 0xFF, 4-remaining);
                      for i in 0..remaining: dest[i]=src[length-remaining+i];
                      length -= remaining; offset = 4
        for i in offset..aligned_len step 4:
            dest[i+0]=src[length-i+offset-4]; ...+1..+3
    """
    length = len(src)
    aligned_len = _align_up(length, 4)
    dest = bytearray(aligned_len)
    remaining = length % 4
    offset = 0
    if remaining > 0:
        for k in range(remaining, 4):
            dest[k] = 0xFF
        for i in range(remaining):
            dest[i] = src[length - remaining + i]
        length -= remaining
        offset = 4
    for i in range(offset, aligned_len, 4):
        dest[i + 0] = src[length - i + offset - 4]
        dest[i + 1] = src[length - i + offset - 3]
        dest[i + 2] = src[length - i + offset - 2]
        dest[i + 3] = src[length - i + offset - 1]
    return bytes(dest)


class XtensaFlasher:
    """Loads + runs an OpenOCD S3 flasher stub over espjtag's XtensaXDM transport.

    `xdm` is an espjtag.xtensa.XtensaXDM (halt/resume + read_mem/write_mem +
    register access). The S3 core MUST be halted before loading/running."""

    def __init__(self, xdm, chip="esp32s3"):
        self.x = xdm
        self.chip = chip
        if chip not in STUBS:
            raise ValueError(f"no stub set for {chip}")
        self.loaded = None            # the cmd currently loaded, or None
        self.stub = {}                # resolved run-state for the loaded stub

    @staticmethod
    def _plan_section(payload, base_address, reverse, dram_org):
        """PURE: the (addr, bytes) writes load_section_from_image would do — same
        1024-byte chunking + reverse logic, but returns the plan instead of
        writing. Lets the layout be validated with NO JTAG."""
        writes = []
        size = len(payload)
        sec_wr = 0
        while sec_wr < size:
            nb = min(1024, size - sec_wr)
            buf = payload[sec_wr:sec_wr + nb]
            if reverse:
                aligned_len = _align_up(len(buf), 4)
                writes.append((dram_org - sec_wr - aligned_len, _reverse_binary(buf)))
            else:
                writes.append((base_address + sec_wr, _align4(buf)))
            sec_wr += len(buf)
        return writes

    def plan_load(self, cmd):
        """PURE 1:1 port of esp_algorithm_load_func_image (reverse S3) that returns
        the memory image as {'writes': [(addr, bytes), ...], 'stub': {...}} WITHOUT
        touching JTAG. load() applies it; the Tcl harness validates it. Memory map
        (esp_algorithm.c:527,469): [code+tramp] reversed into the IRAM area,
        data normal at dram_org, then stack, then mem-args."""
        cfg = STUBS[self.chip][cmd]
        iram_org = cfg["iram_org"]
        dram_org = cfg["dram_org"]
        code = cfg["code"]
        data = cfg["data"]
        bss = cfg["bss_sz"]
        reverse = S3_REVERSED
        writes = []

        # code section -> iram_org (reversed)
        writes += self._plan_section(code, iram_org, reverse, dram_org)
        code_size = _align_up(len(code), 4)

        # trampoline (esp_algorithm.c:586-624)
        tramp = TRAMP_WIN
        al_tramp_size = _align_up(len(tramp), 4)
        if reverse:
            tramp_addr = (dram_org - code_size) - al_tramp_size
            writes.append((tramp_addr, _reverse_binary(tramp)))
        else:
            tramp_addr = iram_org + code_size
            writes.append((tramp_addr, _align4(tramp)))
        tramp_mapped_addr = iram_org + code_size
        code_size += al_tramp_size

        # data section -> dram_org (normal)
        data_addr = dram_org
        writes += self._plan_section(data, data_addr, False, dram_org)
        data_sec_sz = _align_up(len(data), 4)
        bss_sec_sz = _align_up(bss, 4)

        # stack (esp_algorithm.c:679-687)
        stack_size = cfg["stack_default_sz"]
        stack_area_addr = data_addr + data_sec_sz + bss_sec_sz
        stack_addr = stack_area_addr + stack_size
        wa_next = stack_area_addr + stack_size

        stub = {
            "entry": cfg["entry_addr"],
            "tramp_mapped_addr": tramp_mapped_addr,
            "stack_addr": stack_addr,
            "stack_size": stack_size,
            "wa_next": wa_next,
            "trap_entry_addr": cfg["trap_entry_addr"],
            "trap_record_addr": cfg["trap_record_addr"],
            "iram_org": iram_org, "dram_org": dram_org,
        }
        return {"writes": writes, "stub": stub}

    def load(self, cmd):
        """Apply plan_load() to the target (the only JTAG side-effect here)."""
        plan = self.plan_load(cmd)
        for addr, b in plan["writes"]:
            self.x.write_mem(addr, _bytes_to_words(b))
        self.stub = plan["stub"]
        self.loaded = cmd
        return self.stub

    # --- run_image (esp_algorithm.c:144) + start/wait_algorithm ------------
    def run(self, args=(), mem_params=None, timeout_ms=4000):
        """Run the loaded stub. `args` = up to 5 int args (a2..a6). `mem_params` =
        list of {arg: <user-arg index>, size: <bytes>, data: <out bytes or None>}:
        a RAM buffer is allocated for each, its address written into arg[arg]
        (esp_algorithm_run_image:185-198), and read back into the dict's 'out'
        after the run. Returns the stub return code (a2). Mirrors run_image ->
        start_algorithm -> wait_algorithm.

        Resume at tramp_mapped_addr with: a0=0, a1=sp, a8=entry, args a2..a6,
        ps=0x60025, windowbase=0, windowstart=1; VECBASE=trap_entry_addr; halt
        at the stub's exit BREAK; read a2."""
        if self.loaded is None:
            raise RuntimeError("no stub loaded")
        s = self.stub
        args = list(args)
        # mem args: allocate buffers below the stack (descending dram area), write
        # each buffer's address into its user-arg register. (run_image:185-198 — the
        # preloaded path bases mem args at stub.stack_addr and grows up; we place
        # them just below the stack base, clear of the SP, growing down.)
        # mem-args allocate from the work area, continuing the bump (growing UP)
        # after the stack — mirroring target_alloc_working_area (esp_algorithm.c:
        # 168-184 non-preloaded path: alloc a buffer, write its addr into arg[idx]).
        mem_params = mem_params or []
        base = s["wa_next"]
        for mp in mem_params:
            base = _align_up(base, 4)
            mp["_addr"] = base
            idx = mp["arg"]
            while len(args) <= idx:
                args.append(0)
            args[idx] = base
            if mp.get("data"):                                  # PARAM_OUT-to-target
                self.x.write_mem(base, _bytes_to_words(mp["data"]))
            base += _align_up(mp["size"], 4)
        sp = (s["stack_addr"] & ~0xF) - 16                       # algo_regs_init_start
        # Build the reg_params exactly as esp_xtensa_algo_regs_init_start does
        # (esp_xtensa_algorithm.c:47), then call the ONE faithful start/wait_algorithm
        # port in xtensa.py. a2 is inout (the return code). Resume onto the
        # trampoline, which callx8's the windowed entry (a8) natively.
        reg_params = [
            ("vecbase", s["trap_entry_addr"], "out"),
            ("ps", RUN_PS, "out"),                               # WOE+UM+INTLEVEL6
            ("windowbase", 0, "out"),
            ("windowstart", 1, "out"),
            ("a0", 0, "out"),
            ("a1", sp, "out"),
            ("a8", s["entry"], "out"),                           # windowed entry
            ("a2", args[0] if args else 0, "inout"),             # arg0 / return code
        ]
        for i, a in enumerate(args[1:], start=3):                # a3..a6 = args 1..4
            reg_params.append((f"a{i}", a & 0xFFFFFFFF, "out"))
        self.x.start_algorithm(reg_params, s["tramp_mapped_addr"])
        out, halted = self.x.wait_algorithm(reg_params, timeout=timeout_ms)
        if not halted:
            raise RuntimeError("stub did not halt at exit BREAK (timeout)")
        # read back PARAM_IN mem buffers
        for mp in mem_params:
            words = self.x.read_mem(mp["_addr"], _align_up(mp["size"], 4) // 4)
            mp["out"] = b"".join(w.to_bytes(4, "little") for w in words)[:mp["size"]]
        return out["a2"]                                         # return code
