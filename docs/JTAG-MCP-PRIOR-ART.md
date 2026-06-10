# JTAG / embedded-debug MCP servers — prior art for espjtag's MCP server (#14)

Web research, June 2026. Goal: learn from anyone who has exposed a JTAG / SWD /
embedded debugger (or any debugger) as **MCP tools**, so we design espjtag's MCP
server (issue #14) well. Pure desk research — no hardware, no code run.

**Bottom line up front.** There is solid, directly-relevant prior art for *generic*
debug-over-MCP (probe-rs, J-Link, GDB, LLDB, pyOCD-planned) and for *ESP-IDF
build/flash/monitor over MCP* (Espressif shipped this officially in v6.0,
April 2026). But **an ESP32 RISC-V JTAG memory/register/halt debugger exposed as
MCP, pure-Python, no OpenOCD, multi-board, with a one-shot chip-state probe, is
greenfield** — nobody has published one. The closest cousin
(`embedded-debugger-mcp`, probe-rs) does ARM+RISC-V but **not** the Espressif
built-in USB-Serial/JTAG, requires a probe-rs install, and is a single-target
stateful session. The espjtag MCP server we already have (`espjtag/mcp_server.py`)
is, as far as this search found, ahead of the field on its safety model
(per-tool read-only/mutating/reset annotation tiers + state-restoring reads).
Details and citations below.

---

## 1. The landscape — table of existing debug / JTAG MCP servers

| Server | Wraps | Transport to debugger | Tools exposed (verbs) | Session model | Safety model | Hardware target | URL | Maintained? |
|---|---|---|---|---|---|---|---|---|
| **espjtag** (ours) | native pure-Python RISC-V DM driver | direct USB (pyusb) to ESP built-in USB-JTAG | `list_probes`, `idcode`, `diag`, `read_memory`, `probe`, `halt`, `resume`, `read_register`, `write_register`, `write_memory`, `reset_run`, `reset_from_rom` | **open-per-call** (claims USB iface per tool, disposes after) | **3 annotation tiers**: RO / MUT / RESET; read tools restore prior run-state; every tool pins a board | ESP32-C3/C5/C6/H2 USB-Serial/JTAG | `espjtag/mcp_server.py` (this repo) | active (this work) |
| **embedded-debugger-mcp** (Adancurusul) | **probe-rs** native lib (Rust) | links probe-rs crate directly | 22 tools: `list_probes`, `connect`, `probe_info`, `read_memory`, `write_memory`, `halt`, `run`, `reset`, `step`, `set_breakpoint`, `clear_breakpoint`, `flash_erase/program/verify`, `rtt_attach/detach/channels/read/write`, `run_firmware`, `get_status`, `disconnect` | **stateful/long-lived** (`connect`…`disconnect`) | distinguishes destructive (flash erase/program) from read; **no explicit MCP annotations found** | ARM Cortex-M + **RISC-V** via J-Link/ST-Link/CMSIS-DAP | https://github.com/Adancurusul/embedded-debugger-mcp | active, ~107★ |
| **dbgprobe-mcp-server** (es617) | **J-Link** CLI (`JLinkExe`) subprocess; OpenOCD + pyOCD *planned* | subprocess to JLinkExe | `dbgprobe.probes.list/connect/disconnect`, `.reset/.halt/.go/.step/.status`, `.flash`, `.mem.read/.write`, `.breakpoint.set/clear/list`, `.erase` + ELF/SVD/RTT/trace | **stateful** (`connect` once, ops, persists) | doc warns "writes affect real hardware… can brick a device"; plugin exec flagged; **no read-only/destructive split** | J-Link, (CMSIS-DAP/ST-Link via OpenOCD planned) | https://github.com/es617/dbgprobe-mcp-server | active, v0.1.4 Mar 2026, ~6★ |
| **mcp_server_gdb** (pansila) | **GDB** | GDB/MI | `create_session`, `get/close_session`, `start/stop_debugging`, `continue/step/next_execution`, `get/set/delete_breakpoint`, `get_stack_frames`, `get_local_variables`, `get_registers`, `read_memory` | **multi-session** (explicit session IDs) | uniform access, **no RO/destructive split**; local-focused (remote GDB not documented) | local processes; embedded remote not documented | https://github.com/pansila/mcp_server_gdb | active, v0.2.3 Apr 2025, ~66★, Rust |
| **mcp-gdb** (signal-slot) | **GDB** | GDB/MI + raw passthrough | breakpoints, stepping, exec control, examine memory, dump registers, call-stack, source; `gdb_command` raw escape hatch | session-based | no RO/destructive split; remote possible "in theory" but not turnkey | local C/C++; remote via manual setup | https://github.com/signal-slot/mcp-gdb | active |
| **MDB-MCP** (smadi0x86) | **GDB + LLDB** (auto-select) | spawns gdb/lldb | `debugger_start/terminate/list_sessions/command`, plus `gdb_*` and `lldb_*` variants — **raw `*_command(session_id, command)` only** | **multi-session** by `session_id` | **none** — pure command passthrough, no structured verbs, no annotations | local binaries | https://github.com/smadi0x86/MDB-MCP | active, ~63★ |
| **LLDB built-in MCP** (LLVM first-party) | **LLDB** itself | `(lldb) protocol-server start MCP listen://…` | **one tool: `lldb_command`** (debugger-id + command string) — runs any LLDB command | one server per LLDB instance, listens on a port | none beyond LLDB's own; pure passthrough | whatever LLDB targets (incl. remote/embedded via `gdb-remote`) | https://lldb.llvm.org/use/mcp.html | first-party, shipping in LLVM |
| **lldb-mcp / claude_lldb_mcp** (stass, benpm, ankur106 …) | **LLDB** | spawns/controls lldb | start/control/inspect LLDB sessions; mostly command-oriented | session-based | none documented | local binaries | https://github.com/stass/lldb-mcp , https://github.com/benpm/claude_lldb_mcp | community, active |
| **GDB-MCP** (smadi0x86, predecessor of MDB-MCP) | **GDB** | spawns gdb | `debugger_status/start/terminate/list_sessions/command` — raw passthrough | multi-session | none | local binaries | https://github.com/smadi0x86/GDB-MCP | superseded by MDB-MCP |
| **ESP-IDF Tools MCP** (Espressif, **official**) | `idf.py` (NOT a debugger) | stdio, built into idf.py | **tools** = build/flash/set-target/list-devices/status (maps to idf.py subcommands); **resources** = read-only project data | per-project (run from project dir) | uses MCP **tools vs resources** split; no JTAG/halt/memory | ESP32 family — **build/flash/monitor only, no on-chip debug** | https://developer.espressif.com/blog/2026/04/esp-idf-tools-mcp-server/ | official, ESP-IDF v6.0, Apr 2026 |
| **esp-mcp** (horw, community) | `idf.py` | stdio | `build_esp_related_project`, `flash_esp_project`, `fullclean`, `list_esp_serial_ports` | per-call | none; PoC | ESP32 build/flash | https://github.com/horw/esp-mcp | community PoC |
| **x64dbg-automate MCP** | x64dbg | native client | breakpoints/memory/registers for Windows reversing | session | n/a (host RE, not JTAG) | Windows binaries | https://dariushoule.github.io/x64dbg-automate-pyclient/mcp-server/ | active |

Notes on the two "OpenOCD-as-MCP" leads: **nobody has published a clean
OpenOCD-Tcl-RPC→MCP bridge.** dbgprobe-mcp lists OpenOCD as a *planned* backend
behind a uniform tool surface, not as a Tcl-RPC wrapper. OpenOCD does expose a
machine interface (Tcl RPC on TCP **6666**, commands terminated with `0x1a`;
plus telnet 4444 and a GDB server) which *would* be wrappable, but no MCP project
was found doing it (https://openocd.org/doc-release/html/Server-Configuration.html).
This matters for us: OpenOCD-as-MCP is itself greenfish-ish, and our pure-Python
path sidesteps the "shell out to a daemon" complexity those wrappers carry.

---

## 2. Tool-design patterns worth adopting

**Two clear schools of tool granularity emerged:**

1. **Structured verbs** — one MCP tool per debug action with typed params:
   `read_memory(addr,len)`, `read_register(reg)`, `set_breakpoint(...)`, `halt()`,
   `resume()`, `reset()`. Used by the *embedded* servers: `embedded-debugger-mcp`
   (probe-rs), `dbgprobe-mcp`, `mcp_server_gdb`. This is the right model for
   hardware: the params are small and typed (address, count, register name), the
   LLM can't fat-finger debugger syntax, and each tool carries its own annotation.

2. **Raw command passthrough** — a single `*_command(session, "string")` tool that
   forwards arbitrary debugger commands. Used by `MDB-MCP`, **LLDB's own
   first-party MCP** (`lldb_command`), and `GDB-MCP`. Rationale (smadi0x86's
   README): "your LLM client should already know how to use [the debugger]." This
   is powerful and minimal but: (a) the LLM must know exact GDB/LLDB syntax for the
   target arch, (b) you lose per-action annotations — every call is opaque, so the
   client can't tell a `p $pc` from a `set {int}0x40800000 = 0` — which **defeats
   the whole safety story**. Espressif's official IDF server explicitly avoids
   passthrough, mapping each AI tool to a known idf.py subcommand.

**The academic prior art (ChatDBG, arXiv:2403.16354, plasma-umass) lands in
between and is the best evidence on *what the LLM actually needs*:** it gives the
LLM a `debug` function that runs underlying-debugger commands and feeds results
back, PLUS higher-level "explain why this crashed" framing, and a "take the wheel"
loop where the model autonomously issues commands to explore state. Measured
result: a single query produced an actionable fix 67% of the time, 85% with one
follow-up (https://arxiv.org/html/2403.16354v3,
https://github.com/plasma-umass/ChatDBG). Takeaway for us: **low-level structured
verbs (read_memory/read_register/halt) are the right *substrate*, but the model
benefits from at least one higher-level "give me the whole picture" tool** so it
doesn't have to compose ten calls to orient itself. Our `probe` tool (idcode +
halted/running + dpc + a few words of memory in one call) is exactly this and is a
pattern the generic servers lack.

**Verb set that recurs across every embedded server** (the consensus minimum):
`list_probes` · `connect`/select · `read_memory` · `write_memory` ·
`read_register`/`get_registers` · `halt` · `resume`/`run` · `reset` · `step` ·
`set_breakpoint`/`clear_breakpoint` · `status`. RTT and flash are common extras.
espjtag already has the core read/write/halt/resume/reset/register verbs; **the
notable gaps vs the field are `step`, `set_breakpoint`/`clear_breakpoint`, and a
multi-word `write_memory` burst** (we have per-word). Breakpoints/step are a clear
future addition (the RISC-V DM supports them; OpenOCD's `dcsr.step` is already
referenced in our `debug.py`).

**Target/probe selection patterns:**
- probe-rs/J-Link servers: `list_probes` → `connect(serial=…)` binds a *session*.
- **espjtag: every tool requires `usb_path` OR `serial`** (no implicit "first
  device"). With ~9 ESP boards on the bench bus, this is strictly safer than the
  connect-once-then-implicit model — there's no ambiguous "current target" the LLM
  can lose track of across calls. This is a genuine improvement to keep.

---

## 3. Session model

| Model | Who uses it | Trade-off |
|---|---|---|
| **Stateful / long-lived** (`connect`…`disconnect`, server holds the handle) | embedded-debugger-mcp, dbgprobe-mcp, GDB/LLDB session servers | Lower latency, enables breakpoints/step that need persistent state; BUT must handle device-vanished, stuck claims, and a "current target" the LLM forgets. |
| **Open-per-call** (each tool opens, works, disposes) | **espjtag** | Slightly higher latency (re-open USB + re-detect TAP, tens of ms); BUT robust — never holds a claim the client forgot to release, board can be unplugged between calls, no stale session. |

espjtag's open-per-call choice is well-reasoned for *this* hardware: opening an
`EspUsbJtag` **claims the USB vendor interface exclusively**, so two concurrent
opens of the same unit are impossible anyway — a long-lived session would just be a
single global lock with extra failure modes. The documented tradeoff (per-call TAP
re-detect latency) is the right one to accept for a debug-assistant tool surface.
**Caveat for the future:** breakpoints and single-step inherently need persisted
hart state across calls (set BP → resume → it hits → inspect). If/when we add
those, we'll need an opt-in *cached-session* mode (the mcp_server.py comment
already flags this as "a future optimisation"). The probe-rs server's stateful
model is the reference design for that phase.

---

## 4. The MCP spec angle — annotations and the tools-vs-resources split

**MCP exposes three primitives** (modelcontextprotocol.io): **tools**
(model-controlled actions), **resources** (app-controlled read-only context), and
**prompts**. **Tool annotations** are the safety vocabulary
(https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/, MCP blog,
16 Mar 2026):

- `readOnlyHint` (default **false**) — tool does not modify its environment.
- `destructiveHint` (default **true**) — change is destructive vs additive.
  *Only meaningful when `readOnlyHint` is false.*
- `idempotentHint` (default **false**) — safe to call again with same args.
- `openWorldHint` (default **true**) — reaches an open world of external entities.

**Critically, these are HINTS, not contracts.** Per the official blog: "annotations
are informational signals, not enforceable guarantees… clients must treat
annotations from untrusted servers as untrusted." Their primary job is a
**preflight UX question: should the client auto-approve, or ask the user to
confirm, before calling this tool?** A `readOnlyHint:true` tool from a trusted
server may be auto-approved; a `destructiveHint:true` tool should get a confirmation
step. Real safety must live in deterministic controls, not the hint.

**How a debug server *should* use these** (synthesised from the spec + the field —
note **no other debug MCP server in this survey actually sets annotations**, so
this is mostly our own application of the spec):

- **Pure reads** (`list_probes`, `idcode`, `diag`) → `readOnlyHint:true`,
  `destructiveHint:false`, `openWorldHint:false`. Auto-approvable.
- **State-restoring reads** (`read_memory`, `read_register`, `probe` — which briefly
  halt a *running* core then resume) → still effectively read-only from the user's
  intent, but they *do* perturb timing. We mark them `readOnlyHint:true` but the
  description must call out the pause. (Defensible either way; see §6.)
- **Mutating-but-recoverable** (`halt`, `resume`, `write_register`, `write_memory`)
  → `readOnlyHint:false`, `destructiveHint:false`. Perturbs a live target; client
  should confirm but it's not a wipe.
- **Reboot/wipe** (`reset_run`, `reset_from_rom`) → `readOnlyHint:false`,
  `destructiveHint:true`, `idempotentHint:false`. Loses in-progress work; highest
  caution.
- **`openWorldHint:false` on everything** — a JTAG probe is a closed, local domain.

**Espressif's official IDF server is the one real-world example of the
tools-vs-resources discipline**: it exposes *actions* (build/flash) as **tools** and
*read-only project data* as **resources**. The analogue for a debugger: a live,
mutating read (which halts the core) is a **tool**; truly static facts (the probe
list, the chip's IDCODE→name table, register-name map) could be **resources**.
We currently model everything as tools — fine for a first cut, but exposing the
probe list / chip dictionary as resources is a clean future refinement.

---

## 5. Is ESP32-JTAG-over-MCP greenfield? Yes — with caveats

**Greenfield for our exact niche.** No published MCP server debugs the **ESP32
built-in USB-Serial/JTAG** at the memory/register/halt level. What exists nearby:

- **Espressif's own MCP effort (v6.0, official) is build/flash/monitor only** — it
  deliberately does *not* touch JTAG, OpenOCD, halt, memory, or registers
  (https://developer.espressif.com/blog/2026/04/esp-idf-tools-mcp-server/). So the
  vendor has staked out the *project-management* half of ESP+MCP and left the
  *on-chip-debug* half open. That's our lane.
- **`embedded-debugger-mcp` (probe-rs) is the closest functional cousin** — it does
  ARM **and RISC-V** memory/register/halt/breakpoint/RTT as MCP tools. But probe-rs
  does **not** speak the ESP built-in USB-Serial/JTAG (it drives external
  J-Link/ST-Link/CMSIS-DAP probes), it requires a Rust/probe-rs install, and it's a
  single-target stateful session. So even on RISC-V it doesn't cover the
  no-external-probe, pure-Python, multi-board ESP case.
- Every GDB/LLDB MCP server *could* reach an ESP target via
  `riscv32-esp-elf-gdb` + OpenOCD's GDB server — but that's the heavyweight
  OpenOCD+GDB toolchain we're explicitly avoiding, and none of them package it for
  ESP.

**What we do better than the field** (claims we can stand behind from this survey):

1. **No OpenOCD, no external probe, no GDB** — pure-Python over the chip's *own*
   USB-JTAG. Every other embedded debug MCP shells to a daemon (OpenOCD/J-Link) or
   links a native toolkit (probe-rs). Lowest-dependency by far.
2. **Multi-board by construction** — mandatory `usb_path`/`serial` per tool; the
   others bind one "current" target and can debug the wrong board if the LLM loses
   track.
3. **The strongest safety model in the survey** — we're the *only* one applying
   per-tool MCP annotations with a deliberate RO / mutating / reset tier split, and
   the only one with **state-restoring reads** (halt→read→resume to leave run-state
   as found). dbgprobe-mcp only *warns in prose*; the GDB/LLDB ones have no split.
4. **A one-shot `probe` chip-state tool** — the orientation tool ChatDBG's findings
   argue for, which none of the generic servers provide.
5. **ESP-specific reset semantics** — `reset_from_rom` (USB-bus-reset to clear the
   post-flash download-strap latch) encodes hard-won ESP32-C6 behaviour no generic
   probe tool models.

**What the others do that we don't (honest gaps):** breakpoints + single-step
(probe-rs, dbgprobe, GDB servers all have them; we don't yet), **RTT** (real-time
logging channel — very valuable, probe-rs+dbgprobe have it), **flash programming**
as a tool, and burst/multi-word `write_memory`. None are blockers for a debug
*assistant*, but breakpoints+RTT are the obvious next features and both are
feasible on the ESP RISC-V DM.

---

## 6. Concrete recommendations for espjtag's MCP server (#14)

The existing `espjtag/mcp_server.py` already implements most of the right design.
These recommendations **validate what's there against the field** and list the
deltas.

**Keep (validated by prior art):**
- **Open-per-call session model** — correct given exclusive USB-interface claim.
  Document it; don't switch to a stateful pool until breakpoints force it.
- **Mandatory `usb_path` OR `serial` on every device tool** — strictly safer than
  the connect-once model every other server uses. Keep.
- **The RO / MUT / RESET annotation tiers + matching descriptions** — this is ahead
  of the field; keep and make sure descriptions stay blunt ("PAUSES THE RUNNING
  FIRMWARE", "REBOOTS THE TARGET").
- **State-restoring reads** (halt→read→resume) — unique and user-friendly. Keep.
- **The `probe` orientation tool** — backed by ChatDBG's "give the model the whole
  picture" finding. Keep; consider widening it (add a couple of key CSRs like
  mcause/mepc so a crashed core is self-explaining in one call).
- **Structured verbs, not raw passthrough** — matches the embedded-server consensus
  and preserves per-tool annotations (passthrough would destroy the safety story).

**Tool list to converge on** (current + recommended additions):
```
READ-ONLY:      list_probes, idcode, diag, read_memory, read_register, probe
MUTATING:       halt, resume, step*, write_register, write_memory(burst*)
                set_breakpoint*, clear_breakpoint*, list_breakpoints*
DESTRUCTIVE:    reset_run, reset_from_rom
                (* = not yet implemented — clear next steps, all DM-feasible)
```
RTT and flash-program tools are worth tracking as a later milestone (both proven
valuable by probe-rs/dbgprobe), but they're a separate workstream from the core
debugger surface.

**Session model decision:** stay open-per-call for the read/write/halt/resume/reset
surface. **When breakpoints/step land**, add an opt-in cached-session mode (one
persistent `EspUsbJtag` per pinned unit, with device-vanished handling) — model it
on probe-rs's stateful `connect`/`disconnect`. Don't retrofit stateful onto the
read tools; the per-call model is the safer default.

**Safety annotations — final recommended mapping** (using the spec's exact fields,
all `openWorldHint:false`):

| Tool | readOnlyHint | destructiveHint | idempotentHint | Client behaviour |
|---|---|---|---|---|
| list_probes, idcode, diag | true | false | true | auto-approve |
| read_memory, read_register, probe | true | false | true | auto-approve (description flags the brief pause) |
| halt, resume | false | false | true | confirm-ish; cheap to undo |
| write_register, write_memory | false | false | false | **confirm before call** |
| step, set/clear_breakpoint | false | false | false | confirm |
| reset_run, reset_from_rom | false | **true** | false | **strong confirm** |

One judgement call to revisit: `read_memory`/`read_register`/`probe` are marked
`readOnlyHint:true` even though they *briefly halt a running core*. This is the
right default (the user's intent is a read; run-state is restored) — but a
real-time-critical target could be disrupted by the pause. **Recommendation:**
keep `readOnlyHint:true` (so the model treats them as safe to use for
investigation) but ensure the description's pause warning stays prominent, and
consider a server-level config flag `disturb_running_core=false` that makes those
tools *refuse* (return an error) rather than halt a running core, for users who
truly cannot tolerate a stall. That gives the deterministic control the MCP blog
says must back the hint, rather than relying on the hint alone.

**The hint-is-not-a-contract reality** (MCP blog, 16 Mar 2026): because annotations
are advisory, our *real* safety guarantees are the deterministic things we already
do — mandatory board pinning, error envelopes that never crash the stdio session,
readback-after-write confirmation, and (recommended) the refuse-to-disturb flag.
The annotations are the UX layer on top; both layers matter.

---

## Sources

- es617/dbgprobe-mcp-server — https://github.com/es617/dbgprobe-mcp-server (v0.1.4, Mar 2026)
- Adancurusul/embedded-debugger-mcp (probe-rs) — https://github.com/Adancurusul/embedded-debugger-mcp
- probe-rs — https://github.com/probe-rs/probe-rs , https://probe.rs/
- pansila/mcp_server_gdb — https://github.com/pansila/mcp_server_gdb (v0.2.3, Apr 2025)
- signal-slot/mcp-gdb — https://github.com/signal-slot/mcp-gdb
- smadi0x86/MDB-MCP — https://github.com/smadi0x86/MDB-MCP ; predecessor GDB-MCP — https://github.com/smadi0x86/GDB-MCP
- LLDB first-party MCP — https://lldb.llvm.org/use/mcp.html
- stass/lldb-mcp — https://github.com/stass/lldb-mcp ; benpm/claude_lldb_mcp — https://github.com/benpm/claude_lldb_mcp
- x64dbg Automate MCP — https://dariushoule.github.io/x64dbg-automate-pyclient/mcp-server/
- Espressif ESP-IDF Tools MCP (official, v6.0, Apr 2026) — https://developer.espressif.com/blog/2026/04/esp-idf-tools-mcp-server/
- horw/esp-mcp (community PoC) — https://github.com/horw/esp-mcp
- MCP tool annotations, official blog (16 Mar 2026) — https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/
- MCP debugging docs — https://modelcontextprotocol.io/docs/tools/debugging
- OpenOCD Tcl RPC / server config (port 6666) — https://openocd.org/doc-release/html/Server-Configuration.html
- ChatDBG paper (arXiv:2403.16354) — https://arxiv.org/html/2403.16354v3 ; repo — https://github.com/plasma-umass/ChatDBG
- pyOCD debug probes — https://pyocd.io/docs/debug_probes.html
- ESP32-C6 JTAG debugging guide — https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-guides/jtag-debugging/index.html

_Researched June 2026. espjtag MCP server reference: `espjtag/mcp_server.py`, `espjtag/debug.py` (this repo)._
