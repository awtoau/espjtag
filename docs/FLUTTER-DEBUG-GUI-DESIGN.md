# Flutter debug GUI — design

A Flutter app that shows a **live, physical view of an ESP32 board** — per-pin GPIO
state, CPU registers, memory — by speaking an **existing** on-chip-debug protocol
instead of inventing a new wire format. The board sits on a host PC's USB; bench
phones connect to that host over WiFi and render.

Status: **design only.** This enables the build; it is not the build. Step 1 of the
long-term vision (a rotatable 3D/PCB view) is just getting debug *data* into Flutter
reliably, which is what this doc nails down.

> **Refreshed 2026-06-10:** earlier revisions of this doc said `espjtag` could *not*
> do memory/register reads and treated OpenOCD as the only viable backend. That is no
> longer true — `espjtag` now does halt/resume, GPR+CSR, and memory read/write over
> SBA, so it is a viable Shape-B backend today (see `docs/ESPJTAG-STORY.md`). The
> backend-choice claims below have been corrected accordingly.

---

## 1. TL;DR recommendation

```
  ┌─────────────┐  WiFi / TCP   ┌──────────────────────────┐  USB-JTAG  ┌────────┐
  │  Android    │ ◄───────────► │  host PC (bench machine) │ ◄────────► │ ESP32  │
  │  Flutter    │   line/JSON   │  ── debug server ──      │            │  C6 …  │
  │  app (3.44) │               │  OpenOCD (Tcl RPC :6666) │            └────────┘
  └─────────────┘               │  + thin Dart/py adapter  │
                                └──────────────────────────┘
```

- **Protocol to the chip:** **OpenOCD's Tcl RPC** (TCP `:6666`), *not* the GDB remote
  serial protocol. The Tcl RPC is a line-oriented machine interface with first-class
  commands — `halt`, `resume`, `reg`, `get_reg`, `read_memory`, `mdw`, `mww` — whose
  output is trivially parseable. The GDB RSP (`$packet#cc`) is lower level, requires
  ack handling and per-target register-map knowledge, and buys us nothing for a
  *polling* GUI. (See §3 for the head-to-head.)
- **Transport to the phone:** the phone **never touches USB**. It talks **TCP** over
  WiFi to a server on the host. Two viable shapes (§5):
  - **A (simplest, ship first):** Flutter connects *directly* to OpenOCD's Tcl RPC
    port. One socket, no extra process. Works today.
  - **B (cleaner long-term):** a thin **adapter** on the host (Dart `dart:io` or a
    small Python shim) exposes a stable **JSON-over-WebSocket** "board state" feed and
    hides whether the backend is OpenOCD *or* our pure-Python `espjtag`
    (`vendor/espjtag/`). The Flutter app only ever sees board-state JSON.
- **Recommendation:** **build A first** (Flutter → OpenOCD Tcl RPC directly) to prove
  data flow, then **introduce B** when we want backend choice, multi-client fan-out,
  and to decouple the GUI's data model from OpenOCD's text output. A and B share the
  same Flutter `BoardState` model (§6), so A→B is an adapter swap, not a rewrite.

Everything the GUI needs reduces to three primitives, all of which **both** backends
already expose: **halt/resume**, **read-register**, **read-memory-word**. Per-pin GPIO
is just memory reads of the GPIO peripheral registers (§4).

---

## 2. What we already have (reuse, don't reinvent)

| Asset | Path | What it gives us |
| --- | --- | --- |
| Espressif OpenOCD | `~/.espressif/tools/openocd-esp32/v0.12.0-esp32-20251215/.../bin/openocd` (see `scripts/dev/flash.py`) | Full RISC-V + Xtensa debug: halt/resume, regs, memory, over the **built-in USB-JTAG**. PROVEN on C6 (flash + reset-run 3/3). Speaks Tcl RPC, GDB RSP, telnet. |
| `espjtag` (pure Python) | `vendor/espjtag/` (repo `awtoau/espjtag`) | From-scratch pyusb RISC-V Debug-Module client. **Today:** IDCODE, DTMCS, DMI read/write, **halt/resume/examine, GPR+CSR read/write, memory read/write (System Bus Access)**, `reset_run()`, batched bulk reads, `diag()`. So espjtag already serves the three primitives this GUI needs (halt/resume + read-register + read-memory) — it is a **viable Shape-B backend today**, not just OpenOCD. **Not yet:** flash-over-JTAG, batched *writes*. RISC-V only (C3/C5/C6/H2); S2/S3 use a different debug module. (Capability table: espjtag `README.md` "What works".) |
| Bench inventory | `scripts/esp32-devices.json`, `docs/XIAO-PINOUT.md` | Per-chip `Dn → GPIO` maps (authoritative, from each board's Zephyr DT) and per-board wiring — the static half of the pin data model (§6). |
| Board geometry (KiCad) | `docs/hardware/**/*.kicad_pcb`, `*.step`, `~/git/awto-kicad` | PCB/3D source for the phase-3 physical view (cross-ref the separate KiCad-geometry research). |

Flutter house style to match (from the existing repos):

- **State:** Bloc/Cubit (`flutter_bloc ^8`), `equatable` for value-equality models —
  `~/git/awto-flutter-framework/ARCHITECTURE.md`, `CODE_CONVENTIONS.md`.
- **Files:** `snake_case.dart`, classes `PascalCase`, 2-space indent, 80-col,
  immutable `final`/`const` models, error *states* not throws.
- **Transport abstraction prior art:** `ChameleonUltraGUI`'s
  `lib/connector/serial_abstract.dart` — an `AbstractSerial` base with
  `serial_native` / `serial_android` / `serial_ble` / `serial_emulator`
  implementations behind one interface. We mirror that with an
  `AbstractDebugTransport` (OpenOCD-Tcl / espjtag-WS / replay-fixture) — see §6.
- SDK floor in the existing apps is `>=3.0.0 <4.0.0`; this app targets Flutter 3.44.

---

## 3. OpenOCD network interfaces — which one, and why

OpenOCD listens on three TCP ports
([Server Configuration](https://openocd.org/doc/html/Server-Configuration.html)):

| Port | Interface | Audience | Verdict for this GUI |
| --- | --- | --- | --- |
| **3333** | GDB remote serial protocol (RSP) | GDB / IDE debuggers | Standard but low-level; over-kill for polling. **No.** |
| **4444** | telnet / human monitor | a person at a terminal | Echoes prompts/banners; meant for humans, not parsing. **No.** |
| **6666** | **Tcl RPC** (machine interface) | programs / GUIs | Line-oriented, structured commands, easy to parse. **Yes.** |

### 3a. Tcl RPC (`:6666`) — the chosen protocol

Framing
([Tcl Scripting API](https://openocd.org/doc/html/Tcl-Scripting-API.html)): connect
TCP, **send a command string terminated by a single `0x1a` byte**, read the reply up
to the next `0x1a`. Repeat on the same socket. In current OpenOCD you no longer need
the legacy `ocd_` prefix to get a return value, though some text-returning commands
still want `capture`. (Same `0x1a` framing is implemented in the canonical
`contrib/rpc_examples/ocd_rpc_example.py` —
[arduino mirror](https://github.com/arduino/OpenOCD/blob/master/contrib/rpc_examples/ocd_rpc_example.py),
and the maintained `PyOpenocdClient` library —
[GitHub](https://github.com/HonzaMat/PyOpenocdClient).)

Wire transcript (`<SUB>` = the `0x1a` terminator byte):

```
→  halt<SUB>
←  <SUB>                              # empty body, command ok

→  reg pc<SUB>
←  pc (/32): 0x42000010<SUB>          # name (/bits): 0xVALUE

→  mdw 0x6009103c<SUB>                # GPIO_IN_REG on the C6 (see §4)
←  0x6009103c: 00000004<SUB>          # "ADDR: HEXWORD" — split on ": "

→  mdw 0x60091000 4<SUB>              # read 4 words from base
←  0x60091000: 00000000 00000004 00000000 deadbeef<SUB>
```

Preferred **machine-interface** commands (cleaner than the human ones above) — added
specifically so scripts don't have to scrape text
([General Commands](https://openocd.org/doc/html/General-Commands.html),
[read/write_memory patch](https://www.mail-archive.com/openocd-devel@lists.sourceforge.net/msg13821.html)):

```
→  read_memory 0x60091000 32 4<SUB>   # addr, element-width-bits, count
←  0x0 0x4 0x0 0xdeadbeef<SUB>         # Tcl list of ints — split on space

→  get_reg {pc sp ra}<SUB>            # returns a Tcl dict
←  pc 0x42000010 sp 0x40800000 ra 0x42000a1c<SUB>

→  write_memory 0x60091000 32 {0x1}<SUB>
←  <SUB>
```

The command set we actually use (all over `:6666`):

| Intent | Command | Reply shape |
| --- | --- | --- |
| stop the core | `halt` | empty |
| run the core | `resume` | empty |
| single step | `step` | empty |
| state of the target | `riscv.cpu curstate` / poll | `halted` / `running` |
| all registers | `reg` | one `name (/bits): 0xVAL` per line |
| one/few registers | `get_reg {pc sp mstatus}` | Tcl dict `name 0xVAL …` |
| N words from addr | `read_memory <addr> 32 <count>` | space-separated ints |
| one word (legacy) | `mdw <addr>` | `<addr>: <hexword>` |
| write a word | `write_memory <addr> 32 {<val>}` / `mww <addr> <val>` | empty |

For RISC-V parts OpenOCD exposes the standard register names — `zero ra sp gp tp t0…
a0… pc mstatus …`
([riscv.c](https://openocd.org/doc-release/doxygen/riscv_8c_source.html)) — so the GUI
can name registers without a hard-coded map.

### 3b. GDB RSP (`:3333`) — considered, rejected for now

The RSP is a well-specified, widely-implemented protocol
([GDB Remote Protocol — Packets](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Packets.html)):
each packet is `$` + data + `#` + 2-hex-digit checksum (mod-256 sum of the body), and
each side acks with `+`/`-`. Core stubs must support `?` (why halted), `g`/`G`
(read/write all registers, hex, target byte order), `m addr,len` / `M` (read/write
memory). Example: `$m6009103c,4#xx` reads 4 bytes at `0x6009103c`.

Why **not** for this GUI:

- **Register decoding burden.** `g` returns one opaque hex blob; you must know the
  exact target register layout (and `qXfer:features:read` XML target description) to
  slice it. The Tcl `reg`/`get_reg` give *named* values for free.
- **Ack/retransmit + sequencing.** RSP is a stateful conversation (acks, `vCont`,
  stop-replies) designed around *one* controlling debugger. A GUI that just polls
  doesn't want that bookkeeping.
- **No Dart library exists** for either protocol (see §3c), so we implement *something*
  by hand regardless — and hand-writing the line-based Tcl client is far less code than
  a correct RSP client.

We keep RSP in our back pocket for one thing: if we ever want the GUI to **share a
live GDB session** (breakpoints, stepping source), GDB itself can connect to `:3333`
while we read state from `:6666` in parallel — OpenOCD multiplexes them.

### 3c. Is there a Dart/Flutter client for either? — No.

A pub.dev / GitHub search turned up **no** Dart package implementing the GDB remote
serial protocol or an OpenOCD Tcl client. The nearest Dart building blocks are generic:
`dart:io` `Socket`, `web_socket_channel`, and serial packages (`libserialport`,
`flutter_serial_communication`) we don't need (the phone isn't on USB). **Conclusion:**
we write a small Dart Tcl-RPC client (~100 lines: connect, `0x1a` frame, parse). Python
prior art to port the parsing from: `PyOpenocdClient`
([GitHub](https://github.com/HonzaMat/PyOpenocdClient)) and `pyopenocd`
([cdleonard](https://github.com/cdleonard/pyopenocd)).

---

## 4. Getting GPIO state — it's just memory reads

The GUI needs no special GPIO RPC. Per-pin state is read straight from the chip's GPIO
peripheral and IO-MUX registers via `read_memory`/`mdw`. For the **ESP32-C6**
(`DR_REG_GPIO_BASE = 0x60091000`,
[ESP-IDF gpio_reg.h / TRM](https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-reference/peripherals/gpio.html)):

| Register | Address (C6) | Meaning |
| --- | --- | --- |
| `GPIO_OUT_REG` | `0x60091004` | output level driven on each pin (bit per GPIO) |
| `GPIO_ENABLE_REG` | `0x60091020` | output-enable (driver on = output, off = input/Hi-Z) |
| `GPIO_IN_REG` | `0x6009103C` | **input level read back from the pad** (the live pin state) |
| `GPIO_STATUS_REG` | `0x60091044` | interrupt-pending per pin |
| IO-MUX `GPIOn` | `IO_MUX_BASE + 4*(n+1)` | pin function / pull-up/down / drive strength |
| `GPIO_FUNCn_OUT_SEL_CFG` | base `+ 0x0554 + 4*n` | which peripheral signal drives pin n (GPIO matrix) |

So **one `read_memory 0x60091000 32 N`** burst pulls OUT/ENABLE/IN in a few words, and
the GUI derives, per pin: **level** (IN bit), **direction** (ENABLE bit → output/input),
**driven value** (OUT bit), and — with the IO-MUX/func words — **function** (GPIO vs a
peripheral like SPI/UART). Cross-referenced with `docs/XIAO-PINOUT.md` (the `Dn → GPIO`
map) this becomes a physical per-pad picture.

Base addresses differ per SoC (C3/C5/H2/S3 each have their own `DR_REG_GPIO_BASE` and
offsets). Keep a small **per-chip register map** table in the app (or generated from the
ESP-IDF `soc/*/gpio_reg.h` headers in `modules/`), keyed by the IDCODE/chip the backend
reports. **Do not hard-code one chip.**

Caveat: reading peripheral registers over JTAG generally requires the core to be in a
state where the debug module can do system-bus/abstract memory access. **Both** backends
handle this — OpenOCD via its RISC-V driver, `espjtag` via its System Bus Access
`read_mem`/`read_register` path (it can read GPIO banks via SBA without halting). The
open product question (§9) is whether a *running-core* read of the GPIO bank is reliable,
not which backend can issue the read.

---

## 5. Transport from a phone (the phone never touches USB)

The board is on the **host PC's** USB. Android phones can't run OpenOCD or open the
USB-JTAG device. Therefore:

- The **host** runs the debug server (OpenOCD, and later the adapter). USB stays on the
  host.
- The **phone** opens a **TCP socket over WiFi** to the host's IP and renders. That's
  the entire phone-side transport — no USB, no native serial, no platform channels.

Two server shapes:

**Shape A — Flutter → OpenOCD Tcl RPC directly.** Start OpenOCD with the C6 USB-JTAG
config (as `flash.py` already does) plus `tcl_port 6666` and **bind it to the LAN**
(`bindto 0.0.0.0`, default is localhost-only). The phone connects to
`hostip:6666`, sends `0x1a`-framed commands. Zero extra processes. Ship this first.

- *Security:* the Tcl RPC is an **unauthenticated arbitrary-Tcl** endpoint — anyone on
  the LAN who reaches `:6666` can run any OpenOCD command. Fine on a trusted bench LAN;
  bind to the bench subnet, never route it to the open internet. (This alone is a good
  reason to prefer Shape B's narrow JSON surface later.)

**Shape B — Flutter → JSON/WebSocket adapter → {OpenOCD | espjtag}.** A thin host
process (Dart `dart:io` `HttpServer`/`WebSocket`, or a small Python `websockets` shim)
that:

- owns the OpenOCD socket *or* drives `espjtag` directly,
- polls the board on a timer and pushes **`BoardState` JSON** (§6) to all connected
  phones,
- accepts a tiny command set (`halt`, `resume`, `setPin?`, `poll`),
- exposes a **stable schema** independent of OpenOCD's text output and of which backend
  is live.

Shape B wins on: multi-phone fan-out, backend choice (OpenOCD *or* pure-Python
`espjtag` — its memory/register path already exists, so this is just an adapter, no
phone change), a narrow auditable surface instead of arbitrary-Tcl, and a natural place
to merge the **static** pin map
(`esp32-devices.json` / `XIAO-PINOUT.md`) with the **live** register reads.

**Discovery:** the host advertises over mDNS/Bonjour (or the phone takes a typed
`hostip:port`, mirroring ChameleonUltraGUI's `manual_connect.dart`). The bench already
has an `awto-<last4>` naming scheme (see MEMORY) we can reuse for the host service name.

---

## 6. Data model — "board state" the GUI consumes

One immutable, `Equatable`, JSON-serializable model. Identical whether produced by a
direct-Tcl `Repository` (Shape A) or received as adapter JSON (Shape B) — so the UI and
Blocs are transport-agnostic.

```dart
// board_state.dart  — immutable snapshot the UI renders
class BoardState extends Equatable {
  final String chip;            // "esp32c6" — selects the per-chip register map
  final String serial;          // bench id, ties to esp32-devices.json
  final CoreStatus core;        // halted | running | unknown
  final List<PinState> pins;    // one per physical pad / GPIO
  final Map<String, int> registers;   // {"pc": 0x42000010, "sp": ...}
  final List<MemoryRegion> memory;    // raw windows the user opened
  final DateTime sampledAt;     // for staleness display
  const BoardState({ /* ... */ });
}

enum CoreStatus { halted, running, unknown }
enum PinDirection { input, output, disabled }

class PinState extends Equatable {     // derived from GPIO regs + static map
  final int gpio;                 // GPIO number (e.g. 16)
  final String pad;               // "D6" — from XIAO-PINOUT.md
  final int? physicalPin;         // 7 — board footprint position
  final PinDirection direction;   // from GPIO_ENABLE_REG bit
  final bool level;               // from GPIO_IN_REG bit (live pad level)
  final bool drivenValue;         // from GPIO_OUT_REG bit
  final String function;          // "gpio" | "spi.sck" | "uart.tx" … (IO-MUX)
  final String? net;              // "MCP2515 CS" — from esp32-devices.json wiring
  const PinState({ /* ... */ });
}

class MemoryRegion extends Equatable {
  final int baseAddr;
  final List<int> words;          // 32-bit words from read_memory
  final String? label;            // "GPIO bank", "stack", …
  const MemoryRegion({ /* ... */ });
}
```

Two data sources feed it: **live** (register/memory reads each poll) and **static**
(pad↔GPIO map + wiring, loaded once from `esp32-devices.json` / `XIAO-PINOUT.md`). The
repository/adapter joins them so a `PinState` carries both `level` (live) and
`net`/`pad` (static).

Transport abstraction (mirrors ChameleonUltraGUI's `AbstractSerial`):

```dart
abstract class AbstractDebugTransport {
  Future<void> connect(String host, int port);
  Future<void> halt();
  Future<void> resume();
  Future<Map<String,int>> readRegisters(List<String> names);
  Future<List<int>> readMemory(int addr, int count);   // 32-bit words
  Stream<BoardState> states();   // poll loop pushes here
}
// impls: OpenOcdTclTransport, EspJtagWsTransport, ReplayTransport (fixtures/tests)
```

Polling cadence is a **named, justified constant**, not a magic number (per the repo's
time-period rule): e.g. `const gpioPollInterval = Duration(milliseconds: 200)` —
because human-perceptible pin changes don't need faster, and each poll halts/reads/…
the core. Make it configurable and **state the reason in code**. A halted-core poll and
a running-core poll have different costs; document which we use.

---

## 7. Prior art (tool → protocol → takeaway)

| Tool | Frontend | Talks | Takeaway for us |
| --- | --- | --- | --- |
| **gdbgui** ([site](https://www.gdbgui.com/), [repo](https://github.com/cs01/gdbgui)) | web (browser) | spawns GDB, parses **GDB/MI** via `pygdbmi` | Don't drive a debugger's *human* CLI — use a machine interface and parse structured output. We pick OpenOCD's Tcl RPC as our "MI". |
| **cortex-debug** ([repo](https://github.com/Marus/cortex-debug)) | VS Code (DAP) | GDB/MI ⇄ GDB ⇄ **RSP** ⇄ OpenOCD/JLink | Layered: GUI↔adapter↔GDB↔RSP↔server. Confirms RSP is the *low* layer; a GUI rides higher. We collapse layers by reading state straight from OpenOCD. |
| **probe-rs** ([repo](https://github.com/probe-rs/probe-rs)) | CLI + its own GDB server + **DAP** | implements **RSP** + DAP in Rust | A modern tool re-implemented RSP rather than reuse OpenOCD — possible but heavy; its GDB server is "less mature than OpenOCD". Reinforces: use OpenOCD, don't reinvent the JTAG stack. |
| **pyOCD** ([gdbserver](https://pyocd.io/)) | gdbserver + Python API | **RSP** server; Python API for mem/reg | Shows the clean split: a Python API (mem/reg/halt) underneath an optional RSP server. Our Shape-B adapter is the same idea in Dart/py. |
| **PyOpenocdClient** ([repo](https://github.com/HonzaMat/PyOpenocdClient)) | Python lib | **OpenOCD Tcl RPC** (`0x1a` framing) | Direct prior art for *our* client: `halt()`, `resume()`, `read_memory()`, `get_reg()` over `:6666`, stdlib-only. Port its parsing to Dart. |
| **ocd_rpc_example.py** ([arduino mirror](https://github.com/arduino/OpenOCD/blob/master/contrib/rpc_examples/ocd_rpc_example.py)) | example | OpenOCD Tcl RPC | Canonical reference for the framing + `ocd_mdw` "split on `: `" parse. |
| **ChameleonUltraGUI** ([repo](https://github.com/GameTec-live/ChameleonUltraGUI)) | **Flutter** (Android/iOS/desktop) | abstract serial: native/android/ble/emulator | House-style prior art for a hardware-tool Flutter GUI: one `AbstractSerial` interface, per-platform impls, an **emulator** impl for dev without hardware. We copy the pattern (`AbstractDebugTransport` + `ReplayTransport`). |
| **Segger Ozone / SystemView** | Qt desktop | proprietary (JLink) | Aspirational UX (register/peripheral/RTOS views, live waveform) but closed + Qt + JLink-only. Useful as a *visual* target, not a protocol. |

Cross-cutting lesson: **every** serious tool sits a GUI on top of a *parseable*
machine interface (GDB/MI, DAP, or an RPC) and lets an established backend
(GDB+OpenOCD/JLink/probe-rs) own the actual JTAG. We do exactly that with OpenOCD's
Tcl RPC.

---

## 8. Phased plan

**Phase 0 — host plumbing (no Flutter).** Launch OpenOCD on the host with the per-chip
USB-JTAG config (reuse `flash.py`'s invocation) + `tcl_port 6666` + `bindto` the bench
LAN. Verify from the host with a 20-line Python (or `PyOpenocdClient`): `halt`, `reg
pc`, `mdw 0x6009103c`. **Exit:** a phone on the LAN can `nc hostip 6666` and get a
reply. Proves transport before any UI.

**Phase 1 — connect + show registers/memory as text.** Flutter app: connect dialog
(typed `hostip:port`, à la `manual_connect.dart`), a Dart `OpenOcdTclTransport`
(`dart:io` Socket + `0x1a` framing), a `DebugBloc` that polls `get_reg`/`read_memory`,
and plain widgets: a register table and a hex-memory view (ChameleonUltraGUI's
`hex_editor.dart` is a starting point). **Exit:** live `pc`/`sp`/registers and a memory
window update on a real C6 over WiFi. This is "step 1 — debug data into Flutter."

**Phase 2 — per-pin GPIO grid.** Add the per-chip GPIO register map (§4) and the static
pad map (`XIAO-PINOUT.md` / `esp32-devices.json`). Build `PinState` from GPIO
OUT/ENABLE/IN reads; render the XIAO footprint as a grid (the ASCII layout in
`XIAO-PINOUT.md` made visual): each pad colored by level, shaped by direction, labeled
with `pad`/`gpio`/`net`. **Introduce Shape B** here (JSON/WS adapter) so the grid
consumes `BoardState` JSON and the backend can be either OpenOCD or `espjtag` (its
SBA register/memory path is already built). **Exit:** wiggle a
pin on the board (or in firmware) and watch the corresponding pad change on the phone.

**Phase 3 — 3D / PCB view.** Replace the grid with a rotatable physical board. Geometry
from the KiCad/STEP under `docs/hardware/**` and `~/git/awto-kicad` — **cross-reference
the separate KiCad-geometry research** for the extraction pipeline (board outline, pad
positions, component placement → a mesh/glTF or a Flutter `CustomPainter`/3D scene). The
*data* layer is unchanged: `PinState` already carries `physicalPin`/`gpio`/level; phase
3 is purely a new renderer over the same `BoardState`. **Exit:** rotate the board, pads
light with live state.

Each phase ships standalone and reuses the prior phase's model — only the **renderer**
changes between 1→2→3; the transport and `BoardState` are stable from phase 1.

---

## 9. Honest unknowns and risks

- **Reading peripheral regs may need a halted core.** OpenOCD can read memory while
  running on many targets, but RISC-V abstract/system-bus access and ESP32 specifics
  may force a `halt` per poll — which *pauses the firmware* and perturbs exactly the
  GPIO behaviour we're watching. **Open:** measure on the C6 whether `read_memory` of
  the GPIO bank works run-time; if not, decide whether a brief halt is acceptable or
  whether we use the chip's debug-mode memory access. This is the single biggest
  product risk.
- **Poll rate vs intrusiveness.** Fast polling over USB-JTAG + (maybe) halting could
  starve or stall the firmware. Needs a measured, justified interval (§6) and possibly
  a "freeze on halt" UX so the user knows the core stopped.
- **`espjtag` is a viable Shape-B backend today** (this was previously listed as a
  blocker — corrected). It does halt/resume/examine, GPR+CSR read/write, and memory
  read/write over SBA (espjtag `README.md` "What works"), so it can already serve the
  GPIO/register data this GUI needs. We still **build Shape A on OpenOCD first** — not
  because espjtag can't serve the data, but because OpenOCD's Tcl RPC is a ready
  network surface (no adapter to write) and is the proven C6 path; espjtag slots in
  under Shape B's JSON adapter when we want the no-OpenOCD backend. What's genuinely
  unbuilt in espjtag is *flash-over-JTAG* and Xtensa (S2/S3) — neither is on the GUI's
  critical path.
- **Xtensa (S2/S3).** `espjtag` is RISC-V-only; the GPIO register-read approach via
  OpenOCD still works on S3, but the per-chip register map and any DMI assumptions
  differ. Keep the chip map data-driven.
- **Tcl RPC security.** `:6666` is unauthenticated arbitrary Tcl. Bind to the bench
  subnet only; treat Shape A as bench-LAN-only. Shape B's narrow JSON surface is the
  mitigation, not a hardened OpenOCD.
- **Mobile WiFi quirks.** Android battery/doze can drop idle TCP sockets; the app needs
  reconnect/backoff (a `DebugBloc` error state, not a throw). mDNS discovery is flaky on
  some Android/WiFi combos — keep the typed-IP fallback.
- **Per-chip register addresses.** GPIO/IO-MUX base addresses differ across
  C3/C5/C6/H2/S3; getting one wrong silently shows garbage pins. Generate the map from
  ESP-IDF `soc/*/gpio_reg.h` (in `modules/`) rather than transcribing by hand, and unit
  test against a known C6 read.
- **No Dart prior art** for either protocol means we own the client code and its bugs
  (framing edge cases, partial reads across packet boundaries, the `0x1a` split). Small,
  but ours. Mitigate with a `ReplayTransport` over captured fixtures so the UI is
  testable without hardware (ChameleonUltraGUI's `serial_emulator.dart` pattern).

---

## 10. Sources

- OpenOCD User's Guide — [Server Configuration](https://openocd.org/doc/html/Server-Configuration.html),
  [Tcl Scripting API](https://openocd.org/doc/html/Tcl-Scripting-API.html),
  [General Commands](https://openocd.org/doc/html/General-Commands.html).
- OpenOCD RPC examples / libs —
  [ocd_rpc_example.py (arduino mirror)](https://github.com/arduino/OpenOCD/blob/master/contrib/rpc_examples/ocd_rpc_example.py),
  [PyOpenocdClient](https://github.com/HonzaMat/PyOpenocdClient),
  [pyopenocd](https://github.com/cdleonard/pyopenocd),
  [read/write_memory addition](https://www.mail-archive.com/openocd-devel@lists.sourceforge.net/msg13821.html).
- OpenOCD RISC-V register names — [riscv.c](https://openocd.org/doc-release/doxygen/riscv_8c_source.html).
- GDB remote serial protocol —
  [Packets](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Packets.html),
  [Overview](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Overview.html),
  [Embecosm RSP how-to](https://www.embecosm.com/appnotes/ean4/embecosm-howto-rsp-server-ean4-issue-2.html).
- ESP32-C6 GPIO registers —
  [ESP-IDF GPIO (C6)](https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-reference/peripherals/gpio.html),
  [ESP32-C6 Technical Reference Manual](https://www.espressif.com/sites/default/files/documentation/esp32-c6_technical_reference_manual_en.pdf).
- Prior-art GUIs —
  [gdbgui](https://github.com/cs01/gdbgui) / [pygdbmi](https://github.com/jbwdevries/pygdbmi),
  [cortex-debug](https://github.com/Marus/cortex-debug),
  [probe-rs](https://github.com/probe-rs/probe-rs),
  [pyOCD](https://pyocd.io/),
  [ChameleonUltraGUI](https://github.com/GameTec-live/ChameleonUltraGUI).
- In-repo —
  `vendor/espjtag/` (espjtag repo `awtoau/espjtag`; capability table in
  its `README.md`), `scripts/dev/flash.py`,
  `docs/C6-USJ-RESET.md`, `docs/XIAO-PINOUT.md`, `scripts/esp32-devices.json`,
  `docs/hardware/**` (KiCad/STEP); Flutter house style —
  `~/git/awto-flutter-framework/{ARCHITECTURE,CODE_CONVENTIONS}.md`,
  `~/git/ChameleonUltraGUI/chameleonultragui/lib/connector/serial_abstract.dart`.
