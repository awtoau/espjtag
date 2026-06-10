"""espjtag.mcp_server — an MCP server exposing the espjtag RISC-V JTAG debugger
as tools, so an MCP client (Claude Code, the planned VS Code extension #15, etc.)
can debug an ESP32-C3/C5/C6/H2 over its built-in USB-Serial/JTAG.

Run it:  ``python -m espjtag.mcp``  (stdio transport — the MCP default).

Design (see docs/MCP-SERVER.md for the full write-up):

* **Session model = open-per-call.** Opening an ``EspUsbJtag`` *claims* the USB
  vendor interface, and you cannot have two open at once for the same physical
  unit. So every tool opens the device, does its work, and ``dispose_resources``
  before returning. This is the simple, safe, robust choice: the bench has ~9
  boards, a board can be unplugged between calls, and we never hold a claim the
  client forgot to release. The tradeoff is latency — each call re-opens USB and
  re-detects the TAP chain (tens of ms). A cached-session pool would be faster but
  has to handle the device vanishing; that's a future optimisation, not needed for
  a debug-assistant tool surface.

* **Every tool pins the unit.** With many ``303a:1001`` on the bus, a tool that
  "just grabs the first one" would debug a random board. So every device-touching
  tool REQUIRES a ``usb_path`` (the sysfs port chain, e.g. ``"1-1.3.1.3.1"``) or a
  ``serial`` — exactly one. ``list_probes`` enumerates both for every board.

* **read_memory halts then restores.** System Bus Access memory reads need the DM
  examined; to be least-surprising a *read* must not leave the core halted. So
  ``read_memory`` examines, checks ``dmstatus``: if the core was already halted it
  leaves it halted; if it was running it halts, reads, and RESUMES — restoring the
  prior run state. (SBA on these parts works against a running hart too, but
  examining + a clean known state is the robust path and matches the library's
  documented "hart must be halted" contract for registers.) A brief firmware pause
  is unavoidable and is called out in the tool description.

* **Read-only vs mutating is explicit.** Each tool carries MCP ``ToolAnnotations``
  (``readOnlyHint`` / ``destructiveHint``) AND says so in its description, so an
  LLM is cautious about anything that pauses or reboots live firmware.

* **Errors don't crash the server.** A missing board, a busy interface, a bad
  ``usb_path`` — every tool catches it and returns a structured ``{"error": ...}``
  dict instead of letting the exception kill the stdio session.

The ``mcp`` SDK is imported lazily (only when you actually build/run the server)
so ``import espjtag`` still works on a machine without the MCP SDK installed.
"""

import usb.core
import usb.util

from .constants import VID, PID, REG_GPR_BASE, CSR_DPC, CSR_DCSR, DM_ALLHALTED
from .debug import EspUsbJtag


# ===========================================================================
# Device discovery / selection
# ===========================================================================

def _usb_path(dev):
    """Build the transport's usb_path string for a pyusb device: "bus-p.p.p"
    (sysfs port chain). Matches EspUsbJtagTransport._match's parser. A root device
    with no port_numbers degrades to just the bus number."""
    ports = tuple(dev.port_numbers or ())
    if not ports:
        return str(dev.bus)
    return f"{dev.bus}-" + ".".join(str(p) for p in ports)


def _safe_serial(dev):
    try:
        return dev.serial_number
    except Exception:                                  # noqa: BLE001
        return None


def _find_probes():
    """List every 303a:1001 on the bus as plain dicts (no interface claimed)."""
    out = []
    for dev in usb.core.find(find_all=True, idVendor=VID, idProduct=PID):
        out.append({
            "serial": _safe_serial(dev),
            "usb_path": _usb_path(dev),
            "vid": f"0x{VID:04x}",
            "pid": f"0x{PID:04x}",
        })
    return out


def _resolve_path(usb_path=None, serial=None):
    """Turn an (optional) usb_path and/or serial into the single usb_path string
    EspUsbJtag wants. Exactly one unit must match; raises ValueError otherwise so
    a tool never silently debugs the wrong board."""
    if usb_path and serial:
        raise ValueError("pass usb_path OR serial, not both")
    if not usb_path and not serial:
        raise ValueError(
            "must pin a unit: pass usb_path (e.g. '1-1.3.1.3.1') or serial "
            "(call list_probes to see what's on the bus)")
    if usb_path:
        # Validate it actually exists, for a clear error before we try to claim it.
        for p in _find_probes():
            if p["usb_path"] == usb_path:
                return usb_path
        raise ValueError(f"no 303a:1001 at usb_path {usb_path!r} "
                         f"(call list_probes for what's present)")
    # serial -> usb_path
    matches = [p for p in _find_probes() if p["serial"] == serial]
    if not matches:
        raise ValueError(f"no 303a:1001 with serial {serial!r}")
    if len(matches) > 1:
        raise ValueError(f"serial {serial!r} matched {len(matches)} devices "
                         f"(ambiguous); pin by usb_path instead")
    return matches[0]["usb_path"]


def _open(usb_path=None, serial=None):
    """Resolve + open an EspUsbJtag for one pinned unit. Caller disposes."""
    return EspUsbJtag(_resolve_path(usb_path, serial))


# ===========================================================================
# Register-name -> regno mapping
# ===========================================================================
# GPR x_n -> REG_GPR_BASE + n (the abstract-command regno space). CSRs are their
# own number. We accept: x0..x31, the RISC-V ABI names, "pc"/"dpc" (the debug PC
# CSR — the PC the core halted at), "dcsr", and any raw int / "0x.." regno.

_ABI_GPR = {
    "zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4,
    "t0": 5, "t1": 6, "t2": 7,
    "s0": 8, "fp": 8, "s1": 9,
    "a0": 10, "a1": 11, "a2": 12, "a3": 13, "a4": 14, "a5": 15, "a6": 16, "a7": 17,
    "s2": 18, "s3": 19, "s4": 20, "s5": 21, "s6": 22, "s7": 23, "s8": 24,
    "s9": 25, "s10": 26, "s11": 27,
    "t3": 28, "t4": 29, "t5": 30, "t6": 31,
}
# A few CSRs worth a friendly name. "pc" maps to dpc (the only PC readable while
# halted via the DM is the debug-PC CSR).
_CSR_NAMES = {
    "dpc": CSR_DPC, "pc": CSR_DPC, "dcsr": CSR_DCSR,
    "mstatus": 0x300, "mepc": 0x341, "mcause": 0x342, "mtvec": 0x305,
    "mtval": 0x343, "mie": 0x304, "mip": 0x344, "mhartid": 0xF14,
}


def _resolve_regno(name_or_regno):
    """Map a friendly register name OR a raw number to an abstract-command regno.
    Returns (regno, canonical_name). Raises ValueError on an unknown name."""
    if isinstance(name_or_regno, int):
        return name_or_regno, hex(name_or_regno)
    s = str(name_or_regno).strip().lower()
    # raw numeric (decimal or 0x..) -> treat as a literal regno
    try:
        n = int(s, 0)
        return n, hex(n)
    except ValueError:
        pass
    if s in _ABI_GPR:
        return REG_GPR_BASE + _ABI_GPR[s], s
    if s.startswith("x") and s[1:].isdigit():
        n = int(s[1:])
        if 0 <= n <= 31:
            return REG_GPR_BASE + n, s
        raise ValueError(f"GPR out of range: {name_or_regno!r} (x0..x31)")
    if s in _CSR_NAMES:
        return _CSR_NAMES[s], s
    raise ValueError(
        f"unknown register {name_or_regno!r}: use x0..x31, an ABI name "
        f"(ra/sp/a0..), pc/dpc/dcsr, a known CSR, or a raw regno like 0x7b1")


def _coerce_int(value):
    """Accept an int or a string like '0x42000000' / '1078001664'."""
    if isinstance(value, int):
        return value
    return int(str(value), 0)


# ===========================================================================
# MCP server construction (lazy import of the mcp SDK)
# ===========================================================================

def build_server():
    """Construct and return the FastMCP server with all espjtag tools registered.
    The mcp SDK is imported HERE (not at module import) so `import espjtag` works
    without it."""
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    mcp = FastMCP(
        "espjtag",
        instructions=(
            "RISC-V JTAG debugger for ESP32-C3/C5/C6/H2 over the built-in "
            "USB-Serial/JTAG. Call list_probes() first to see which boards are on "
            "the USB bus, then pass a board's usb_path (or serial) to every other "
            "tool. READ-ONLY tools (list_probes, idcode, diag, read_memory, probe) "
            "do not disturb the running firmware. MUTATING tools (halt, resume, "
            "read_register, write_register, write_memory, reset_run, "
            "reset_from_rom) PAUSE or RESET the running target — only use them when "
            "the user intends to perturb the live system."),
    )

    RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                         openWorldHint=False)
    # Mutating-but-recoverable (halt/resume/register/memory writes): not read-only,
    # not "destructive" in the wipe-data sense, but they perturb a live target.
    MUT = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                          openWorldHint=False)
    # Reset tools reboot the target — flag destructive so a client is most cautious.
    RESET = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                            idempotentHint=False, openWorldHint=False)

    def _err(e):
        """Uniform error envelope — never raise out of a tool (would kill stdio)."""
        return {"error": f"{type(e).__name__}: {e}"}

    # ---------------------------------------------------------------- READ-ONLY

    @mcp.tool(
        annotations=RO,
        description=(
            "READ-ONLY. List every ESP32 USB-Serial/JTAG probe (303a:1001) on the "
            "USB bus. Returns one entry per board with its serial, usb_path (the "
            "sysfs port chain like '1-1.3.1.3.1'), vid and pid. Pass a returned "
            "usb_path (or serial) to every other tool to pin which board you mean. "
            "Touches no JTAG state — safe at any time."),
    )
    def list_probes() -> dict:
        try:
            probes = _find_probes()
            return {"count": len(probes), "probes": probes}
        except Exception as e:                         # noqa: BLE001
            return _err(e)

    @mcp.tool(
        annotations=RO,
        description=(
            "READ-ONLY. Read the JTAG IDCODE of the pinned board (e.g. 0x0000dc25 "
            "= ESP32-C6, 0x00017c25 = C5). Resets the TAP only; does not disturb "
            "the running firmware. Pin the board with usb_path or serial."),
    )
    def idcode(usb_path: str = "", serial: str = "") -> dict:
        try:
            j = _open(usb_path or None, serial or None)
            try:
                ic = j.read_idcode()
                return {"usb_path": _resolve_path(usb_path or None, serial or None),
                        "idcode": f"0x{ic:08x}"}
            finally:
                usb.util.dispose_resources(j.dev)
        except Exception as e:                         # noqa: BLE001
            return _err(e)

    @mcp.tool(
        annotations=RO,
        description=(
            "READ-ONLY. Dump the RISC-V Debug Module registers of the pinned board: "
            "IDCODE, dmcontrol, dmstatus (with allhalted/allrunning decoded), and "
            "hartinfo. This is the safe pre-flight check before any mutating "
            "operation — it does NOT halt, reset, or otherwise perturb the running "
            "app. Pin the board with usb_path or serial."),
    )
    def diag(usb_path: str = "", serial: str = "") -> dict:
        try:
            j = _open(usb_path or None, serial or None)
            try:
                idc, dmcontrol, dmstatus = j.diag(log=lambda *_: None)
                return {
                    "idcode": f"0x{idc:08x}",
                    "dmcontrol": f"0x{dmcontrol:08x}",
                    "dmstatus": f"0x{dmstatus:08x}",
                    "dmstatus_version": dmstatus & 0xF,
                    "allhalted": bool((dmstatus >> 9) & 1),
                    "allrunning": bool((dmstatus >> 11) & 1),
                }
            finally:
                usb.util.dispose_resources(j.dev)
        except Exception as e:                         # noqa: BLE001
            return _err(e)

    @mcp.tool(
        annotations=RO,
        description=(
            "READ-ONLY (state-restoring). Read nwords 32-bit words from target "
            "memory starting at addr, via System Bus Access. Returns the words as "
            "hex strings. addr may be an int or a hex string like '0x42000000' "
            "(memory-mapped flash) or '0x40800000' (SRAM). This examines the Debug "
            "Module and, IF the core was running, BRIEFLY halts it for the read and "
            "then RESUMES it — restoring the prior run state. The pause is short but "
            "real-time-sensitive firmware will see a stall. If the core was already "
            "halted it stays halted. Pin the board with usb_path or serial."),
    )
    def read_memory(addr, nwords: int = 1, usb_path: str = "",
                    serial: str = "") -> dict:
        try:
            a = _coerce_int(addr)
            n = int(nwords)
            if n <= 0:
                return {"error": "nwords must be >= 1"}
            j = _open(usb_path or None, serial or None)
            try:
                st = j.examine()
                was_running = not (st & DM_ALLHALTED)
                if was_running:
                    j.halt()
                words = j.read_mem(a, n)
                if was_running:
                    j.resume()
                return {
                    "addr": f"0x{a:08x}",
                    "nwords": n,
                    "words": [f"0x{w:08x}" for w in words],
                    "halted_for_read": was_running,
                    "core_state": "resumed" if was_running else "left halted",
                }
            finally:
                usb.util.dispose_resources(j.dev)
        except Exception as e:                         # noqa: BLE001
            return _err(e)

    @mcp.tool(
        annotations=RO,
        description=(
            "READ-ONLY (state-restoring). A one-shot chip-state summary of the "
            "pinned board: IDCODE, whether the core is halted or running, the dpc "
            "(the PC, valid while halted), and a few words of memory at an optional "
            "addr (default the flash mapping 0x42000000). To read the PC and "
            "registers the core must be halted, so if it was running this BRIEFLY "
            "halts, samples, and RESUMES it (restoring run state). Good first look "
            "at an unknown target. Pin the board with usb_path or serial."),
    )
    def probe(usb_path: str = "", serial: str = "", addr="0x42000000",
              nwords: int = 4) -> dict:
        try:
            a = _coerce_int(addr)
            n = max(1, int(nwords))
            j = _open(usb_path or None, serial or None)
            try:
                ic = j.read_idcode()
                st = j.examine()
                was_running = not (st & DM_ALLHALTED)
                if was_running:
                    j.halt()
                dpc = j.read_register(CSR_DPC)
                words = j.read_mem(a, n)
                if was_running:
                    j.resume()
                return {
                    "idcode": f"0x{ic:08x}",
                    "was_running": was_running,
                    "core_state": "resumed" if was_running else "halted (left as found)",
                    "dpc": f"0x{dpc:08x}",
                    "mem_addr": f"0x{a:08x}",
                    "mem_words": [f"0x{w:08x}" for w in words],
                }
            finally:
                usb.util.dispose_resources(j.dev)
        except Exception as e:                         # noqa: BLE001
            return _err(e)

    # ----------------------------------------------------------------- MUTATING

    @mcp.tool(
        annotations=MUT,
        description=(
            "MUTATING — PAUSES THE RUNNING FIRMWARE. Halt the pinned board's RISC-V "
            "core and leave it halted (it stays stopped until you call resume or "
            "reset). Use before read_register/write_register or when you want the "
            "core stopped. Real-time firmware will stop responding while halted. "
            "Pin the board with usb_path or serial."),
    )
    def halt(usb_path: str = "", serial: str = "") -> dict:
        try:
            j = _open(usb_path or None, serial or None)
            try:
                j.examine()
                ok = j.halt()
                return {"halted": bool(ok)}
            finally:
                usb.util.dispose_resources(j.dev)
        except Exception as e:                         # noqa: BLE001
            return _err(e)

    @mcp.tool(
        annotations=MUT,
        description=(
            "MUTATING. Resume the pinned board's core from a halted state so the "
            "firmware runs again. Safe to call if already running. Pin the board "
            "with usb_path or serial."),
    )
    def resume(usb_path: str = "", serial: str = "") -> dict:
        try:
            j = _open(usb_path or None, serial or None)
            try:
                j.examine()
                ok = j.resume()
                return {"running": bool(ok)}
            finally:
                usb.util.dispose_resources(j.dev)
        except Exception as e:                         # noqa: BLE001
            return _err(e)

    @mcp.tool(
        annotations=MUT,
        description=(
            "MUTATING — BRIEFLY PAUSES FIRMWARE. Read one 32-bit RISC-V register of "
            "the pinned board. The 'reg' argument accepts a GPR (x0..x31), an ABI "
            "name (ra, sp, gp, tp, t0..t6, s0..s11/fp, a0..a7), pc/dpc (the halted "
            "PC), dcsr, a known CSR name (mstatus, mepc, mcause, ...), or a raw "
            "regno like '0x7b1'. The core must be halted to read registers: if it "
            "was running this halts, reads, and RESUMES it (restoring run state); if "
            "already halted it is left halted. Pin the board with usb_path/serial."),
    )
    def read_register(reg, usb_path: str = "", serial: str = "") -> dict:
        try:
            regno, canon = _resolve_regno(reg)
            j = _open(usb_path or None, serial or None)
            try:
                st = j.examine()
                was_running = not (st & DM_ALLHALTED)
                if was_running:
                    j.halt()
                val = j.read_register(regno)
                if was_running:
                    j.resume()
                return {
                    "register": canon,
                    "regno": f"0x{regno:x}",
                    "value": f"0x{val:08x}",
                    "halted_for_read": was_running,
                }
            finally:
                usb.util.dispose_resources(j.dev)
        except Exception as e:                         # noqa: BLE001
            return _err(e)

    @mcp.tool(
        annotations=MUT,
        description=(
            "MUTATING — CHANGES TARGET STATE. Write a 32-bit value to a RISC-V "
            "register of the pinned board. 'reg' takes the same names as "
            "read_register (x0..x31, ABI names, pc/dpc, dcsr, CSR names, raw "
            "regno). 'value' is an int or hex string like '0xdeadbeef'. The core "
            "must be halted; this HALTS it first if running and LEAVES IT HALTED "
            "(a register write only makes sense on a stopped core — call resume "
            "afterwards to run). Writing pc/dpc changes where the core resumes. Pin "
            "the board with usb_path or serial."),
    )
    def write_register(reg, value, usb_path: str = "", serial: str = "") -> dict:
        try:
            regno, canon = _resolve_regno(reg)
            v = _coerce_int(value)
            j = _open(usb_path or None, serial or None)
            try:
                j.examine()
                j.halt()                               # register write needs halt
                err = j.write_register(regno, v)
                readback = j.read_register(regno)
                return {
                    "register": canon,
                    "regno": f"0x{regno:x}",
                    "wrote": f"0x{v & 0xFFFFFFFF:08x}",
                    "readback": f"0x{readback:08x}",
                    "cmderr": err,
                    "core_state": "left halted (call resume to run)",
                }
            finally:
                usb.util.dispose_resources(j.dev)
        except Exception as e:                         # noqa: BLE001
            return _err(e)

    @mcp.tool(
        annotations=MUT,
        description=(
            "MUTATING — WRITES TARGET MEMORY. Write one or more 32-bit words to "
            "target memory of the pinned board starting at addr, via System Bus "
            "Access. 'words' is a list of ints or hex strings (e.g. "
            "['0xcafebabe', '0x1234']). addr is an int or hex string. Writes to "
            "SRAM (e.g. 0x40800000) take effect immediately and can corrupt a "
            "running program — only do this with intent. Flash is not writable this "
            "way. This examines the DM but does not halt the core. Pin the board "
            "with usb_path or serial."),
    )
    def write_memory(addr, words, usb_path: str = "", serial: str = "") -> dict:
        try:
            a = _coerce_int(addr)
            if isinstance(words, (int, str)):
                words = [words]
            vals = [_coerce_int(w) & 0xFFFFFFFF for w in words]
            if not vals:
                return {"error": "words must be a non-empty list"}
            j = _open(usb_path or None, serial or None)
            try:
                j.examine()
                for i, v in enumerate(vals):
                    j.write_mem32(a + 4 * i, v)
                # read back for confirmation
                readback = j.read_mem(a, len(vals))
                return {
                    "addr": f"0x{a:08x}",
                    "wrote": [f"0x{v:08x}" for v in vals],
                    "readback": [f"0x{w:08x}" for w in readback],
                    "ok": readback == vals,
                }
            finally:
                usb.util.dispose_resources(j.dev)
        except Exception as e:                         # noqa: BLE001
            return _err(e)

    @mcp.tool(
        annotations=RESET,
        description=(
            "MUTATING — REBOOTS THE TARGET. Full-system reset (pulse ndmreset) then "
            "run the app on the pinned board — reboots a RUNNING core into a fresh "
            "boot. This is OpenOCD's `reset run` equivalent. The firmware restarts "
            "from its reset vector; any in-progress work is lost. Does NOT clear the "
            "post-flash ROM-download strap latch (use reset_from_rom for that). Pin "
            "the board with usb_path or serial."),
    )
    def reset_run(usb_path: str = "", serial: str = "") -> dict:
        try:
            j = _open(usb_path or None, serial or None)
            # reset_run disposes its own handle internally.
            j.reset_run(log=None)
            return {"reset": "run", "ok": True}
        except Exception as e:                         # noqa: BLE001
            return _err(e)

    @mcp.tool(
        annotations=RESET,
        description=(
            "MUTATING — USB BUS RESET + REBOOT. Boot a freshly-flashed ESP32-C6 OUT "
            "of post-flash USB-Serial/JTAG ROM download mode into its app — the "
            "case plain reset_run cannot do. It performs a USB BUS RESET (the JTAG "
            "handle re-enumerates) then the ndmreset+resume handshake. Use this "
            "after flashing with esptool '--after no-reset' when the chip is stuck "
            "in the downloader. C6-proven on Linux; macOS USB-reset is a no-op so it "
            "will not work there. Reboots the target. Pin the board with usb_path or "
            "serial."),
    )
    def reset_from_rom(usb_path: str = "", serial: str = "") -> dict:
        try:
            j = _open(usb_path or None, serial or None)
            ran = j.reset_run_from_rom(log=None)
            return {"reset": "from_rom", "resumed": bool(ran)}
        except Exception as e:                         # noqa: BLE001
            return _err(e)

    return mcp


def main():
    """Entry point for `python -m espjtag.mcp`. Runs the server over stdio."""
    build_server().run()          # transport defaults to "stdio"


if __name__ == "__main__":
    main()
