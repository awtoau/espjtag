#!/usr/bin/env python3
"""ocd_tcl_bridge.py — run OpenOCD's verbatim config Tcl, backed entirely by espjtag.

Design (locked in): mini_jimtcl (pure Python, zero dependencies) as the sole Tcl
engine + OpenOCD's leaf hardware commands (mww/mdw/riscv dmi_read|write/poll)
implemented HERE on espjtag's own transport. No OpenOCD process, no RPC — anything
espjtag lacks, we write ourselves.

CHIP-AWARE: the SAME Tcl runs on either core. mww/mdw use the RISC-V System Bus on a
C6, or the Xtensa XDM (espjtag.xtensa — instruction injection) on an S3. So:
  * C6: OpenOCD's esp32c6_wdt_disable / esp32c6_soc_reset procs (verbatim from
    tcl/target/esp32c6.cfg) run UNMODIFIED — no transcription into chips.py, no drift.
  * S3: `mdw`/`mww` reach memory over the Xtensa XDM, so an S3 OpenOCD config runs the
    same way (demonstrated below: `mdw 0x40000000 4` -> the OpenOCD/probe-rs golden).

SPEED — and this matters: espjtag is ALREADY faster than OpenOCD/probe-rs. Its
_dmi_batch issues a whole burst behind ONE IR=DMI select, FIFO-chunked at the
device's IN limit, instead of OpenOCD's per-access round-trips. So this bridge runs
OpenOCD's verbatim Tcl AT espjtag's (faster) speed: bulk `mdw` routes through the
batched read_mem; scattered `mww` (wdt_disable's regs aren't contiguous) is one
write each in OpenOCD too — no regression. PARALLEL, not a replacement: espjtag's
native chips.py/debug.py paths stay primary; this is the SAME logic via the upstream
Tcl, kept side-by-side until proven — a correctness hedge, NOT because it's slower.

Validate against OpenOCD -d3 (the golden trace), not flaky device values.

Usage:  python3 scripts/ocd_tcl_bridge.py --usb 1-1.3.3.3 [--reset]
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import usb.util

from espjtag import EspUsbJtag, chips
from espjtag.constants import DMCONTROL, DMSTATUS, SBCS, SBADDRESS0, SBDATA0
from espjtag.xtensa import XtensaXDM
from mini_jimtcl import MiniJimTcl

# verbatim from openocd-esp32 tcl/target/esp32c6.cfg
ESP32C6_WDT_DISABLE = r"""
proc esp32c6_wdt_disable { } {
    if { [string compare [command mode] config] == 0 } { return }
    mww 0x60008064 0x50D83AA1
    mww 0x60008048 0
    mww 0x6000807C 0x2
    mww 0x60009064 0x50D83AA1
    mww 0x60009048 0
    mww 0x6000907C 0x2
    mww 0x600b1c18 0x50D83AA1
    mww 0x600B1c00 0
    mww 0x600b1c20 0x50D83AA1
    mww 0x600b1c1c 0x40000000
    mww 0x600B1c30 0xC0000000
}
"""

ESP32C6_SOC_RESET = r"""
proc esp32c6_soc_reset { } {
    global _RISCV_DMCONTROL _RISCV_SB_CS _RISCV_SB_ADDR0 _RISCV_SB_DATA0
    riscv dmi_write $_RISCV_DMCONTROL 0x80000001
    riscv dmi_write $_RISCV_SB_CS 0x48000
    riscv dmi_write $_RISCV_SB_ADDR0 0x600b1034
    riscv dmi_write $_RISCV_SB_DATA0 0x80000000
    riscv dmi_write $_RISCV_DMCONTROL 0
    riscv dmi_write $_RISCV_SB_CS 0x48000
    riscv dmi_write $_RISCV_SB_ADDR0 0x600b1038
    riscv dmi_write $_RISCV_SB_DATA0 0x10000000
    riscv dmi_write $_RISCV_DMCONTROL 0
    riscv dmi_write $_RISCV_DMCONTROL 0x40000001
    sleep 10
    poll
    esp32c6_wdt_disable
    riscv dmi_write $_RISCV_DMCONTROL 0x40000001
    riscv dmi_write $_RISCV_DMCONTROL 0x80000003
}
"""


class OcdTclBridge:
    """A MiniJimTcl with OpenOCD's leaf hardware commands wired to espjtag."""

    def __init__(self, j):
        self.j = j
        self.trace = []                     # (op, addr, val) — for -d3 comparison
        # j is None in --mock mode: only the pure + mock_* commands work (no JTAG).
        if j is None:
            self.core = "mock"
            self.xdm = None
        else:
            # On a RISC-V part mww/mdw use System Bus Access; on Xtensa (S3) they
            # route to the XDM (instruction injection) — same Tcl, right hardware.
            self.core = (chips.lookup(j.read_idcode()) or {}).get("core", "riscv")
            self.xdm = XtensaXDM(j) if self.core == "xtensa" else None
        self.tcl = MiniJimTcl()
        t = self.tcl
        t.register("mww", self._mww)
        t.register("mdw", self._mdw)
        t.register("riscv", self._riscv)
        t.register("command", lambda a: "exec")
        t.register("poll", self._poll)
        t.register("sleep", self._sleep)
        t.register("echo", lambda a: (print(*a), "")[1])
        # Command naming convention: processor-extension commands are
        # `<extension>_<noun>_<verb>` (e.g. xtensa_core_halt, xtensa_stub_load) so
        # the name alone says which extension + what it acts on; generic test/perf
        # commands are plain `noun_verb` (assert_equal, time_mark). A user never
        # needs to read the implementation to understand a command.

        # --- Xtensa extension: stub flasher (#29) ---
        # Drive the OpenOCD prebuilt flasher stub via Tcl, backed by
        # espjtag.xtensa_flasher (the 1:1 port of esp_algorithm_*).
        self._flasher = None
        t.register("xtensa_core_halt", self._xtensa_core_halt)
        t.register("xtensa_stub_load", self._xtensa_stub_load)
        t.register("xtensa_stub_run", self._xtensa_stub_run)
        # PURE plan validators — NO JTAG. plan_load() computes the memory image
        # (addresses + bytes) the load WOULD write; these expose it so the layout
        # is asserted independently of hardware (where transcription bugs hide).
        t.register("xtensa_stub_plan_address", self._xtensa_stub_plan_address)
        t.register("xtensa_stub_plan_bytes", self._xtensa_stub_plan_bytes)
        t.register("xtensa_stub_plan_writes", self._xtensa_stub_plan_writes)
        t.register("xtensa_plan_assert_no_overlap",
                   self._xtensa_plan_assert_no_overlap)
        # --- Xtensa extension: NO-HARDWARE mock (MockXtensaXDM) ---
        # Run the flasher against the software model and assert on the recorded
        # memory/registers/operation-sequence — the full no-chip test path.
        self._mock = None
        t.register("xtensa_mock_load", self._xtensa_mock_load)
        t.register("xtensa_mock_run", self._xtensa_mock_run)
        t.register("xtensa_memory_expect", self._xtensa_memory_expect)
        t.register("xtensa_register_expect", self._xtensa_register_expect)
        t.register("xtensa_operation_count", self._xtensa_operation_count)

        # --- generic test + performance instrumentation (no extension prefix) ---
        t.register("value_equal", self._value_equal)
        t.register("assert_equal", self._assert_equal)
        t.register("assert_less_than", self._assert_less_than)
        t.register("time_mark", self._time_mark)
        t.register("time_elapsed_ms", self._time_elapsed_ms)
        t.register("jtag_transaction_count", self._jtag_transaction_count)
        self._marks = {}
        if self.core == "riscv":
            # the DMI-register globals esp32c6_soc_reset reads (just the DMI
            # addresses espjtag already knows), and the verbatim C6 procs.
            t.vars.update({
                "_RISCV_DMCONTROL": str(DMCONTROL), "_RISCV_SB_CS": str(SBCS),
                "_RISCV_SB_ADDR0": str(SBADDRESS0), "_RISCV_SB_DATA0": str(SBDATA0),
            })
            t.eval(ESP32C6_WDT_DISABLE)
            t.eval(ESP32C6_SOC_RESET)

    # --- OpenOCD leaf commands, on espjtag's transport (RISC-V SBA or Xtensa XDM) -
    def _mww(self, args):
        addr, val = int(args[0], 0), int(args[1], 0)
        self.trace.append(("mww", addr, val))
        (self.xdm.write_mem32 if self.xdm else self.j.write_mem32)(addr, val)
        return ""

    def _mdw(self, args):
        addr = int(args[0], 0)
        n = int(args[1]) if len(args) > 1 else 1
        if self.xdm:
            vals = self.xdm.read_mem(addr, n)
        else:
            vals = self.j.read_mem(addr, n) if n > 1 else [self.j.read_mem32(addr)]
        return " ".join(f"0x{v:08x}" for v in vals)

    def _riscv(self, args):
        if args[0] == "dmi_read":
            addr = int(args[1], 0)
            data, _ = self.j.dmi_read(addr)
            self.trace.append(("dmi_read", addr, data))
            return str(data)
        if args[0] == "dmi_write":
            addr, val = int(args[1], 0), int(args[2], 0)
            self.trace.append(("dmi_write", addr, val))
            self.j.dmi_write(addr, val)
            return ""
        raise RuntimeError(f"riscv: unsupported subcommand {args[0]!r}")

    def _poll(self, args):
        return f"0x{self.j.dm_read(DMSTATUS):08x}"

    def _sleep(self, args):
        # Repo rule: NO wall-clock sleep. OpenOCD's `sleep 10; poll` waits for the
        # SoC reset to propagate; espjtag relies on USB latency + the following
        # poll instead (its native reset path does the same). Intentional no-op.
        return ""

    # --- Xtensa stub-flasher Tcl commands (#29) -------------------------------
    # Register ROLES: a user writes the role, not the raw a-register number, so
    # the test reads meaningfully. (Xtensa passes function args/results in a-regs;
    # the trampoline contract assigns these roles — esp_xtensa_algo_regs_init.)
    _REGISTER_ROLE = {
        "return_address": "a0",      # a0 — return addr (the BREAK trap)
        "stack_pointer": "a1",       # a1 — stack pointer
        "return_value": "a2",        # a2 — arg0 in / return code out
        "entry_point": "a8",         # a8 — the windowed entry the trampoline calls
    }

    def _xtensa_flasher(self):
        if self.xdm is None:
            raise RuntimeError("stub flasher needs an Xtensa (S3) target")
        if self._flasher is None:
            from espjtag.xtensa_flasher import XtensaFlasher
            self._flasher = XtensaFlasher(self.xdm, "esp32s3")
        return self._flasher

    def _xtensa_core_halt(self, args):
        """xtensa_core_halt — power up + halt the Xtensa core. Returns 1 if halted."""
        self.xdm.powerup()
        return "1" if self.xdm.halt() else "0"

    def _xtensa_stub_load(self, args):
        """xtensa_stub_load <command> — load a prebuilt flasher stub (e.g.
        cmd_test1, cmd_flash_map_get) into target RAM over JTAG. Returns
        'entry_point stack_pointer trampoline' addresses."""
        st = self._xtensa_flasher().load(args[0])
        return (f"0x{st['entry']:x} 0x{st['stack_addr']:x} "
                f"0x{st['tramp_mapped_addr']:x}")

    def _xtensa_stub_run(self, args):
        """xtensa_stub_run [arg ...] — run the loaded stub with integer args.
        Returns the stub return value (hex), or 'TIMEOUT' if it did not halt."""
        a = tuple(int(x, 0) for x in args)
        try:
            rc = self._xtensa_flasher().run(args=a, timeout_ms=3000)
        except RuntimeError:
            return "TIMEOUT"
        return f"0x{rc:x}"

    def _plan(self, command):
        # plan_load is PURE (only reads the stub table); a flasher with NO target
        # is fine — it never touches JTAG.
        from espjtag.xtensa_flasher import XtensaFlasher
        return XtensaFlasher(None, "esp32s3").plan_load(command)

    def _xtensa_stub_plan_address(self, args):
        """xtensa_stub_plan_address <command> <name> — PURE (no JTAG). A planned
        address by name: entry | tramp_mapped_addr | stack_addr | dram_org |
        iram_org | trap_entry_addr. Validates the layout without a chip."""
        return f"0x{self._plan(args[0])['stub'][args[1]]:x}"

    def _xtensa_stub_plan_writes(self, args):
        """xtensa_stub_plan_writes <command> — PURE. Number of (address, bytes)
        writes the load would perform."""
        return str(len(self._plan(args[0])["writes"]))

    def _xtensa_stub_plan_bytes(self, args):
        """xtensa_stub_plan_bytes <command> <write_index> <count> — PURE. First
        <count> bytes (hex) of planned write <write_index> — to golden-check the
        reversed code / normal data without touching silicon."""
        i, n = int(args[1]), int(args[2])
        return self._plan(args[0])["writes"][i][1][:n].hex()

    def _xtensa_plan_assert_no_overlap(self, args):
        """xtensa_plan_assert_no_overlap <command> — PURE. PASS if no two planned
        writes overlap (auto-catches layout collisions, e.g. stack/buffer overlap)."""
        p = self._plan(args[0])
        spans = sorted((a, a + len(b)) for a, b in p["writes"])
        bad = None
        for i in range(1, len(spans)):
            if spans[i][0] < spans[i - 1][1]:
                bad = (spans[i - 1], spans[i])
                break
        ok = bad is None
        print(f"  [{'PASS' if ok else 'FAIL'}] {args[0]} plan no-overlap"
              + ("" if ok else f" — {hex(bad[0][0])}..{hex(bad[0][1])} vs {hex(bad[1][0])}"))
        return "1" if ok else "0"

    # --- Xtensa NO-HARDWARE mock (MockXtensaXDM) ------------------------------
    def _xtensa_mock_flasher(self):
        from espjtag.xtensa_mock import MockXtensaXDM
        from espjtag.xtensa_flasher import XtensaFlasher
        if self._mock is None:
            self._mock = MockXtensaXDM()
            self._mock_flasher = XtensaFlasher(self._mock, "esp32s3")
        return self._mock_flasher

    def _xtensa_mock_load(self, args):
        """xtensa_mock_load <command> — load a stub against the software model
        (records writes, NO JTAG). Returns the write count."""
        self._xtensa_mock_flasher().load(args[0])
        return str(len(self._mock.writes))

    def _xtensa_mock_run(self, args):
        """xtensa_mock_run <return_value> [arg ...] — script the stub's return
        value, then run against the model (records the register setup + resume,
        NO JTAG). Returns the value the flasher read back."""
        fl = self._xtensa_mock_flasher()
        self._mock.set_run_result(int(args[0], 0))
        rc = fl.run(args=tuple(int(x, 0) for x in args[1:]))
        return f"0x{rc:x}"

    def _xtensa_memory_expect(self, args):
        """xtensa_memory_expect <address> <hex_bytes> — assert the model RAM at
        <address> equals the golden bytes (validates reversed code / normal data)."""
        addr = int(args[0], 0)
        want = bytes.fromhex(args[1])
        words = self._mock.read_mem(addr, (len(want) + 3) // 4)
        got = b"".join(w.to_bytes(4, "little") for w in words)[:len(want)]
        ok = got == want
        print(f"  [{'PASS' if ok else 'FAIL'}] memory@0x{addr:x} == {args[1]}"
              + ("" if ok else f" (got {got.hex()})"))
        return "1" if ok else "0"

    def _xtensa_register_expect(self, args):
        """xtensa_register_expect <role> <value> — assert the run set the register
        for <role>. role is a meaningful name (entry_point | stack_pointer |
        return_value | return_address), not a raw a-register number."""
        role, want = args[0], int(args[1], 0)
        reg = self._REGISTER_ROLE.get(role, role)
        got = self._mock.regs.get(reg)
        ok = got == want
        print(f"  [{'PASS' if ok else 'FAIL'}] register {role} ({reg}) == 0x{want:x}"
              + ("" if ok else f" (got {got if got is None else hex(got)})"))
        return "1" if ok else "0"

    def _xtensa_operation_count(self, args):
        """xtensa_operation_count [kind] — number of model operations, total or of
        a given kind (write_mem | read_mem | set_ar | set_sr | nar_write). Proves
        the operation SEQUENCE/shape the flasher issued."""
        if args:
            return str(sum(1 for o in self._mock.ops if o[0] == args[0]))
        return str(len(self._mock.ops))

    # --- generic test + performance instrumentation --------------------------
    def _value_equal(self, args):
        """value_equal <a> <b> — 1 if the two strings are equal, else 0."""
        return "1" if args[0] == args[1] else "0"

    def _assert_equal(self, args):
        """assert_equal <label> <got> <want> — print PASS/FAIL; returns 1/0."""
        ok = args[1] == args[2]
        print(f"  [{'PASS' if ok else 'FAIL'}] {args[0]} -> "
              f"{args[1]}" + ("" if ok else f" (want {args[2]})"))
        return "1" if ok else "0"

    def _assert_less_than(self, args):
        """assert_less_than <label> <got> <limit> — PASS if float(got) < limit."""
        ok = float(args[1]) < float(args[2])
        print(f"  [{'PASS' if ok else 'FAIL'}] {args[0]} -> {args[1]} "
              f"< {args[2]}" + ("" if ok else " (TOO SLOW)"))
        return "1" if ok else "0"

    def _time_mark(self, args):
        """time_mark <name> — record a timestamp + transaction count for a span."""
        import time as _t
        self._marks[args[0]] = (_t.perf_counter(), len(self.trace))
        return ""

    def _time_elapsed_ms(self, args):
        """time_elapsed_ms <name> — milliseconds since time_mark <name> (3dp)."""
        import time as _t
        if args[0] not in self._marks:
            return "0"
        return f"{(_t.perf_counter() - self._marks[args[0]][0]) * 1000:.3f}"

    def _jtag_transaction_count(self, args):
        """jtag_transaction_count [name] — total bridge transactions, or those
        since time_mark <name>. (Counts mww/mdw/dmi — the JTAG round-trip proxy.)"""
        if args and args[0] in self._marks:
            return str(len(self.trace) - self._marks[args[0]][1])
        return str(len(self.trace))

    def run(self, tcl):
        return self.tcl.eval(tcl)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb", help="usb_path of the target (omit with --mock)")
    ap.add_argument("--mock", action="store_true",
                    help="NO HARDWARE: run only the pure + mock_* commands "
                         "(the flasher against MockXtensaXDM). For --tcl tests.")
    ap.add_argument("--reset", action="store_true",
                    help="also run esp32c6_soc_reset (RESETS the chip)")
    ap.add_argument("--tcl", help="run a Tcl test script through the bridge "
                    "(e.g. tests for the S3 stub flasher) and exit")
    args = ap.parse_args()

    if args.mock:
        bridge = OcdTclBridge(None)
        print("MOCK mode — no hardware; pure + mock_* commands only")
        if not args.tcl:
            print("(nothing to run; pass --tcl <script>)")
            return 0
        with open(args.tcl) as f:
            bridge.run(f.read())
        return 0

    if not args.usb:
        ap.error("--usb is required (or use --mock)")
    j = EspUsbJtag(args.usb)
    ic = j.read_idcode()
    br = OcdTclBridge(j)
    print(f"IDCODE=0x{ic:08x} [{chips.name_for(ic) or '??'}]  core={br.core}")

    if args.tcl:
        with open(args.tcl) as f:
            script = f.read()
        try:
            br.run(script)
        finally:
            if br.xdm:
                br.xdm.resume()
            usb.util.dispose_resources(j.dev)
        return 0

    if br.core == "xtensa":
        # S3: leaf commands run on the Xtensa XDM. Demonstrate a Tcl `mdw` end-to-end.
        br.xdm.powerup()
        if not br.xdm.halt():
            print("FAILED to halt"); return 1
        print("halted via XDM. Running 'mdw 0x40000000 4' through mini_jimtcl:")
        out = br.run("mdw 0x40000000 4")
        golden = "0x1049c500 0xe52049d5 0x49f53049 0x00003400"
        print(f"  mdw -> {out}")
        print(f"  golden {golden}  {'MATCH' if out == golden else 'MISMATCH'}")
        br.xdm.resume()
        usb.util.dispose_resources(j.dev)
        return 0 if out == golden else 1

    # RISC-V (C6): run OpenOCD's verbatim wdt_disable, read back, optional soc_reset.
    j.examine()
    if not j.halt(disable_wdt=False):       # halt WITHOUT espjtag's native WDT disable
        print("FAILED to halt"); return 1
    print("halted (native WDT-disable skipped — the bridge will do it)")
    print("\nrunning OpenOCD's VERBATIM esp32c6_wdt_disable via espjtag...")
    br.run("esp32c6_wdt_disable")
    for op, a, v in br.trace:
        print(f"  {op} 0x{a:08x} 0x{v:08x}")
    print("\nread-back (proves the writes landed on real silicon):")
    for name, reg, want in (("TG0 cfg", 0x60008048, 0), ("TG1 cfg", 0x60009048, 0),
                            ("LP_WDT_SWD cfg", 0x600B1C1C, 0x40000000)):
        got = j.read_mem32(reg)
        print(f"  {name:14} 0x{reg:08x} = 0x{got:08x}  {'ok' if got == want else 'MISMATCH'}")
    if args.reset:
        print("\nrunning esp32c6_soc_reset (verbatim)...")
        br.run("esp32c6_soc_reset")
        print(f"  dmstatus now 0x{j.dm_read(DMSTATUS):08x}")
    else:
        j.resume()
        print("\nresumed (pass --reset to also run the verbatim soc_reset)")
    usb.util.dispose_resources(j.dev)
    return 0


if __name__ == "__main__":
    sys.exit(main())
