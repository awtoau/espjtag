# espjtag MCP server

`espjtag.mcp_server` exposes the espjtag RISC-V JTAG debugger as
[Model Context Protocol](https://modelcontextprotocol.io) tools, so an MCP client
— Claude Code, the planned VS Code extension ([#15](https://github.com/awtoau/espjtag/issues/15)),
or any other — can halt, inspect, and reset an ESP32-C3/C5/C6/H2 over its built-in
USB-Serial/JTAG, with no OpenOCD binary in the loop.

This is the backend the VS Code extension (#15) will drive, and the start of the
chip-state probe in [#11](https://github.com/awtoau/espjtag/issues/11) (the
`probe` tool). Tracking issue: [#14](https://github.com/awtoau/espjtag/issues/14).

## Run it

```sh
pip install pyusb mcp        # mcp = the official Python SDK
python -m espjtag.mcp        # serves MCP over stdio (the default transport)
```

The server speaks MCP over **stdio** — it is launched by the client as a
subprocess and talks JSON-RPC on stdin/stdout, so you normally don't run it by
hand; you register it with your client (below). You need permission to access the
USB device (a udev rule for `303a:1001`, or run as root).

The `mcp` SDK is imported lazily, so `import espjtag` (and the rest of the library)
still works on a machine without the SDK installed — you only need `mcp` to run
the server.

## Pinning a board

The bench can have many `303a:1001` probes on one USB host. **Every
device-touching tool requires you to pin exactly one unit**, by either:

- `usb_path` — the sysfs port chain, e.g. `"1-1.3.1.3.1"` (bus-port.port.port).
  Stable across reboots for a fixed physical port. This is the canonical id.
- `serial` — the board's USB serial (a MAC-like string). Convenient, but must be
  unique on the bus.

Call **`list_probes`** first; it returns both for every board.

## Tools

Read-only tools do **not** perturb the running firmware. Mutating tools **pause or
reset** the target — each is annotated `readOnlyHint=false` (and the reset tools
`destructiveHint=true`) and says so in its description, so an LLM is cautious.

### Read-only (safe on a live system)

| Tool | What it does |
|---|---|
| `list_probes()` | List every `303a:1001` probe on the USB bus: `{serial, usb_path, vid, pid}`. No JTAG access. |
| `idcode(usb_path/serial)` | JTAG IDCODE of the pinned board (e.g. `0x0000dc25` = C6). TAP reset only. |
| `diag(usb_path/serial)` | RISC-V Debug Module dump: IDCODE, dmcontrol, dmstatus (with halted/running decoded), hartinfo. The safe pre-flight check. |
| `read_memory(usb_path/serial, addr, nwords=1)` | Read `nwords` 32-bit words from `addr` via System Bus Access. **State-restoring**: examines the DM and, if the core was running, *briefly halts, reads, and resumes* (restoring run state). |
| `probe(usb_path/serial, addr=0x42000000, nwords=4)` | One-shot chip-state summary: IDCODE, halted/running, `dpc` (the PC), and a few memory words. Halts+resumes if running. |

### Mutating (perturb the target — only with intent)

| Tool | What it does |
|---|---|
| `halt(usb_path/serial)` | Stop the core and **leave it halted** until resume/reset. Firmware stops responding. |
| `resume(usb_path/serial)` | Run the core again from a halted state. |
| `read_register(usb_path/serial, reg)` | Read a 32-bit register. Needs the core halted: halts+resumes if it was running. |
| `write_register(usb_path/serial, reg, value)` | Write a register. **Halts the core and leaves it halted** (a register write only makes sense stopped — call `resume` after). |
| `write_memory(usb_path/serial, addr, words)` | Write 32-bit words to memory (SRAM) via SBA. Can corrupt a running program. |
| `reset_run(usb_path/serial)` | Full-system reset (pulse ndmreset) then run — reboots a running core. OpenOCD `reset run` equivalent. |
| `reset_from_rom(usb_path/serial)` | USB **bus** reset + ndmreset+resume — boots a freshly-flashed C6 out of post-flash ROM download mode. C6-proven on Linux; macOS USB-reset is a no-op. |

`reg` accepts: GPRs `x0`..`x31`; ABI names `ra sp gp tp t0..t6 s0..s11 fp a0..a7`;
`pc`/`dpc` (the halted PC), `dcsr`; common CSR names (`mstatus mepc mcause mtvec
…`); or a raw regno like `0x7b1`. `addr`/`value`/`words` accept ints or hex
strings (`"0x42000000"`).

Errors (missing board, busy interface, bad `usb_path`) come back as
`{"error": "..."}` — a bad call never crashes the server.

## Design decisions

- **Session = open-per-call.** Opening `EspUsbJtag` *claims* the USB vendor
  interface and you cannot have two open at once for one unit, so every tool
  opens, works, and `dispose_resources` before returning. Simple and robust (a
  board can be unplugged between calls; we never hold a claim the client forgot to
  release). The tradeoff is latency: each call re-opens USB and re-detects the TAP
  chain (tens of ms). A cached session pool would be faster but must handle the
  device vanishing — a future optimisation, not needed for a debug-assistant
  surface.
- **`read_memory`/`read_register`/`probe` halt then restore.** A *read* should not
  leave the core halted. So they examine the DM, and **if the core was running**
  they halt, read, and **resume** — restoring the prior run state; if it was
  already halted they leave it halted. A brief firmware pause is unavoidable and
  is called out in the tool description.
- **Read-only vs mutating is explicit** via MCP `ToolAnnotations`
  (`readOnlyHint` / `destructiveHint`) and in every description.

## Add it to an MCP client

### Claude Code

```sh
claude mcp add espjtag -- python -m espjtag.mcp
```

or, by editing the MCP config (`~/.claude.json` / project `.mcp.json`) directly:

```jsonc
{
  "mcpServers": {
    "espjtag": {
      "command": "python",
      "args": ["-m", "espjtag.mcp"],
      // if espjtag isn't pip-installed, point Python at the repo:
      "env": { "PYTHONPATH": "/home/dan/git/espjtag" }
    }
  }
}
```

### Generic MCP client (Claude Desktop, etc.)

Same shape — a stdio server entry that runs `python -m espjtag.mcp`:

```json
{
  "mcpServers": {
    "espjtag": {
      "command": "python",
      "args": ["-m", "espjtag.mcp"],
      "env": { "PYTHONPATH": "/path/to/espjtag" }
    }
  }
}
```

Then ask the client to `list_probes`, and pass a returned `usb_path` to the other
tools.

## Verification status

- **No-hardware**: import-clean (lazy `mcp` import), all 12 tools register with the
  correct read-only/mutating annotations, register-name mapping, and error
  envelopes — verified.
- **Bench**: smoke-tested on a live **ESP32-C6** (`xiao-c6`, usb_path
  `1-1.3.1.3.1`) through the real `FastMCP.call_tool` path AND through a real
  stdio MCP client subprocess: `list_probes` (9 boards), `idcode` (`0x0000dc25`),
  `diag`, `read_memory` (flash, halt+resume), `probe` (dpc `0x400208b6`). The
  board was left running. The mutating write/reset tools are logically correct and
  exercised by code review but were **not** fired at a live board to avoid
  perturbing bench state — TODO: bench-verify `halt`/`resume`/`write_register`/
  `write_memory`/`reset_run` end-to-end.
```
