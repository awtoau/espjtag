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
        # S3 stub-flasher (#29): drive the OpenOCD prebuilt flasher stub via Tcl,
        # backed by espjtag.xtensa_flasher (the 1:1 port of esp_algorithm_*). Lets
        # the #29 step-tests run as Tcl scripts through this bridge (the OpenOCD
        # way) instead of hand-rolled python.
        self._flasher = None
        t.register("stub_load", self._stub_load)
        t.register("stub_run", self._stub_run)
        t.register("xhalt", self._xhalt)
        # PURE validators — NO JTAG. plan_load() computes the memory image (addrs +
        # bytes) the load WOULD write; these expose it so Tcl can assert the layout
        # is right independent of hardware (the part paraphrase-bugs hid in).
        t.register("stub_plan", self._stub_plan)
        t.register("compare", self._compare)
        # Instrumentation: the same harness MEASURES, not just pass/fails. `mark`/
        # `elapsed` time a span; `jtag_count` reports transactions; `assert_lt`/
        # `assert_eq` gate on thresholds. (Perf becomes a Tcl-scriptable test.)
        t.register("mark", self._mark)
        t.register("elapsed", self._elapsed)
        t.register("jtag_count", self._jtag_count)
        t.register("assert_eq", self._assert_eq)
        t.register("assert_lt", self._assert_lt)
        self._marks = {}
        # MOCK-backed commands — run the flasher against MockXtensaXDM (NO JTAG),
        # then assert on the recorded ops/mem/regs. The full no-hardware test path.
        self._mock = None
        t.register("mock_load", self._mock_load)
        t.register("mock_run", self._mock_run)
        t.register("mem_expect", self._mem_expect)
        t.register("reg_expect", self._reg_expect)
        t.register("op_count", self._op_count)
        t.register("plan_no_overlap", self._plan_no_overlap)
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

    # --- S3 stub-flasher Tcl commands (#29) -----------------------------------
    def _flasher_obj(self):
        if self.xdm is None:
            raise RuntimeError("stub flasher needs an Xtensa (S3) target")
        if self._flasher is None:
            from espjtag.xtensa_flasher import XtensaFlasher
            self._flasher = XtensaFlasher(self.xdm, "esp32s3")
        return self._flasher

    def _xhalt(self, args):
        """xhalt — powerup + halt the Xtensa core. Returns 1 on halted."""
        self.xdm.powerup()
        return "1" if self.xdm.halt() else "0"

    def _stub_load(self, args):
        """stub_load <cmd> — load a prebuilt flasher stub (e.g. cmd_test1,
        cmd_flash_map_get). Returns 'entry stack tramp' addresses."""
        st = self._flasher_obj().load(args[0])
        return (f"0x{st['entry']:x} 0x{st['stack_addr']:x} "
                f"0x{st['tramp_mapped_addr']:x}")

    def _stub_run(self, args):
        """stub_run [arg ...] — run the loaded stub with int args (a2..a6).
        Returns the stub return code (a2) as 0xNN, or 'TIMEOUT' if it didn't halt."""
        a = tuple(int(x, 0) for x in args)
        try:
            rc = self._flasher_obj().run(args=a, timeout_ms=3000)
        except RuntimeError:
            return "TIMEOUT"
        return f"0x{rc:x}"

    def _stub_plan(self, args):
        """stub_plan <cmd> <field> — PURE (no JTAG). field is a stub address
        (entry|tramp_mapped_addr|stack_addr|dram_org|...), or:
          nwrites          — number of (addr,bytes) writes the load would do
          waddr <i>        — hex address of write i
          wbytes <i> <n>   — first n bytes of write i, hex (to golden-check the
                             reversed code / normal data without touching silicon)
        Lets Tcl validate the load LAYOUT deterministically."""
        # plan_load is PURE (only reads STUBS). Make a flasher with NO target so
        # this validator never touches JTAG. (x=None is fine — plan_load never
        # uses self.x.)
        from espjtag.xtensa_flasher import XtensaFlasher
        fl = XtensaFlasher(None, "esp32s3")
        p = fl.plan_load(args[0])
        field = args[1]
        if field == "nwrites":
            return str(len(p["writes"]))
        if field == "waddr":
            return f"0x{p['writes'][int(args[2])][0]:x}"
        if field == "wbytes":
            i, n = int(args[2]), int(args[3])
            return p["writes"][i][1][:n].hex()
        return f"0x{p['stub'][field]:x}"

    def _compare(self, args):
        """compare <a> <b> — returns 1 if equal (string), else 0. For Tcl asserts."""
        return "1" if args[0] == args[1] else "0"

    # --- instrumentation: perf measurement as Tcl ----------------------------
    def _mark(self, args):
        """mark <name> — record a timestamp + jtag-transaction count for a span."""
        import time as _t
        self._marks[args[0]] = (_t.perf_counter(), len(self.trace))
        return ""

    def _elapsed(self, args):
        """elapsed <name> — ms since `mark <name>` (string, 3dp). 0 if no mark."""
        import time as _t
        if args[0] not in self._marks:
            return "0"
        t0, _ = self._marks[args[0]]
        return f"{(_t.perf_counter() - t0) * 1000:.3f}"

    def _jtag_count(self, args):
        """jtag_count [name] — total leaf transactions, or those since `mark name`.
        (Counts mww/mdw/dmi via self.trace; the proxy for JTAG round-trips.)"""
        if args and args[0] in self._marks:
            return str(len(self.trace) - self._marks[args[0]][1])
        return str(len(self.trace))

    def _assert_eq(self, args):
        """assert_eq <label> <got> <want> — print PASS/FAIL."""
        ok = args[1] == args[2]
        print(f"  [{'PASS' if ok else 'FAIL'}] {args[0]} -> "
              f"{args[1]}" + ("" if ok else f" (want {args[2]})"))
        return "1" if ok else "0"

    def _assert_lt(self, args):
        """assert_lt <label> <got> <limit> — PASS if float(got) < float(limit)."""
        ok = float(args[1]) < float(args[2])
        print(f"  [{'PASS' if ok else 'FAIL'}] {args[0]} -> {args[1]} "
              f"< {args[2]}" + ("" if ok else " (TOO SLOW)"))
        return "1" if ok else "0"

    # --- MOCK-backed commands (NO JTAG) --------------------------------------
    def _mock_fl(self):
        from espjtag.xtensa_mock import MockXtensaXDM
        from espjtag.xtensa_flasher import XtensaFlasher
        if self._mock is None:
            self._mock = MockXtensaXDM()
            self._mock_flasher = XtensaFlasher(self._mock, "esp32s3")
        return self._mock_flasher

    def _mock_load(self, args):
        """mock_load <cmd> — load a stub against the MOCK (records writes, no JTAG).
        Returns the write count."""
        fl = self._mock_fl()
        fl.load(args[0])
        return str(len(self._mock.writes))

    def _mock_run(self, args):
        """mock_run <result> [arg ...] — script the stub's a2 return = <result>,
        then run against the mock (records the start/wait_algorithm reg dance +
        resume). Returns the a2 the flasher read back (should equal <result>)."""
        fl = self._mock_fl()
        self._mock.set_run_result(int(args[0], 0))
        a = tuple(int(x, 0) for x in args[1:])
        rc = fl.run(args=a)
        return f"0x{rc:x}"

    def _mem_expect(self, args):
        """mem_expect <addr> <hexbytes> — assert the MOCK model RAM at addr equals
        the golden bytes (validates the reversed code / normal data landed)."""
        addr = int(args[0], 0)
        want = bytes.fromhex(args[1])
        nwords = (len(want) + 3) // 4
        words = self._mock.read_mem(addr, nwords)
        got = b"".join(w.to_bytes(4, "little") for w in words)[:len(want)]
        ok = got == want
        print(f"  [{'PASS' if ok else 'FAIL'}] mem@0x{addr:x} == {args[1]}"
              + ("" if ok else f" (got {got.hex()})"))
        return "1" if ok else "0"

    def _reg_expect(self, args):
        """reg_expect <a-reg> <val> — assert a register the run set (e.g. a8 a1)."""
        name, want = args[0], int(args[1], 0)
        got = self._mock.regs.get(name)
        ok = got == want
        print(f"  [{'PASS' if ok else 'FAIL'}] reg {name} == 0x{want:x}"
              + ("" if ok else f" (got {got if got is None else hex(got)})"))
        return "1" if ok else "0"

    def _op_count(self, args):
        """op_count [kind] — number of mock ops (write_mem/read_mem/set_ar/...),
        total or of a given kind. Proves the op SEQUENCE/shape."""
        if args:
            return str(sum(1 for o in self._mock.ops if o[0] == args[0]))
        return str(len(self._mock.ops))

    def _plan_no_overlap(self, args):
        """plan_no_overlap <cmd> — PURE: assert no two planned writes overlap
        (auto-catches layout collisions like the stack/buffer-overlap bug)."""
        from espjtag.xtensa_flasher import XtensaFlasher
        p = XtensaFlasher(None, "esp32s3").plan_load(args[0])
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
