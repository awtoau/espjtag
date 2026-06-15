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


def _reverse_binary(buf):
    """reverse_binary(): word-wise reverse for the S3 reversed-memory data write
    (esp_algorithm.c). Byte order within each word is preserved (both buses are
    little-endian); only the word ORDER is reversed across the span."""
    out = bytearray(_align_up(len(buf), 4))
    # pad to word, then reverse whole-span byte order? No: OpenOCD reverses the
    # buffer as a byte array of aligned length (reverse_binary in helper/binarybuffer
    # reverses bytes), and writes at dram_org - off - aligned_len. The net effect
    # is the word at the lowest data address maps to the highest instr address.
    padded = bytes(buf) + b"\x00" * (len(out) - len(buf))
    out[:] = padded[::-1]
    return bytes(out)


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

    # --- load_func_image (esp_algorithm.c:498) -----------------------------
    def load(self, cmd):
        """Load the stub for `cmd` into S3 RAM exactly as esp_algorithm_load_
        func_image does for the reversed-memory S3: one IRAM area from iram_org
        holding [code + trampoline], data (reversed) to dram_org, then stack.

        Sets self.stub = {entry, tramp_mapped_addr, stack_addr, trap_entry_addr,
        trap_record_addr, dram_org, ...}. Returns it."""
        cfg = STUBS[self.chip][cmd]
        iram_org = cfg["iram_org"]
        dram_org = cfg["dram_org"]
        code = cfg["code"]
        data = cfg["data"]
        # NO Mach-O parsing: esp_flash.c:341-358 does image_open(...,"build") then
        # image_add_section(code, EXEC) + image_add_section(data) — the cfg blobs
        # are added as RAW section payloads (base 0), then loaded to iram_org/
        # dram_org. The cefaedfe bytes at the start of `code` are the stub's OWN
        # content, not a wrapper OpenOCD strips. So: write code->iram_org,
        # data->dram_org (reversed for the S3). Verbatim from esp_algorithm.c.
        # 1. code -> iram_org
        self.x.write_mem(iram_org, _bytes_to_words(code))
        code_size = _align_up(len(code), 4)
        # 2. trampoline -> right after code; entry to resume at = iram_org+code_size
        self.x.write_mem(iram_org + code_size, _bytes_to_words(TRAMP_WIN))
        tramp_mapped_addr = iram_org + code_size
        code_size += _align_up(len(TRAMP_WIN), 4)
        # 3. data -> dram_org (reversed for the S3) ; bss follows (zeroed by stub)
        if S3_REVERSED:
            rev = _reverse_binary(data)
            self.x.write_mem(dram_org - len(rev), _bytes_to_words(rev))
        else:
            self.x.write_mem(dram_org, _bytes_to_words(data))
        # 4. stack: top of dram area, grows down (OpenOCD allocs in data area).
        #    stack_addr = base + stack_size; we place it below the data region.
        stack_size = cfg["stack_default_sz"]
        stack_addr = dram_org - _align_up(len(data), 4) - 0x10
        stack_addr &= ~0xF                                       # 16-align
        self.stub = {
            "entry": cfg["entry_addr"],
            "tramp_mapped_addr": tramp_mapped_addr,
            "stack_addr": stack_addr,
            "stack_size": stack_size,
            "trap_entry_addr": cfg["trap_entry_addr"],
            "trap_record_addr": cfg["trap_record_addr"],
            "iram_org": iram_org, "dram_org": dram_org,
        }
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
        mem_params = mem_params or []
        base = s["stack_addr"] - s["stack_size"] - 0x10
        for mp in mem_params:
            base = (base - _align_up(mp["size"], 4)) & ~0xF
            mp["_addr"] = base
            idx = mp["arg"]
            while len(args) <= idx:
                args.append(0)
            args[idx] = base
            if mp.get("data"):                                  # PARAM_OUT-to-target
                self.x.write_mem(base, _bytes_to_words(mp["data"]))
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
