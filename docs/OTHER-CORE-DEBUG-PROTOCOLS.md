# Debugging the OTHER cores: Xtensa (ESP32-S2/S3) and Nordic nRF

Strategy + protocol notes for extending our debug tooling beyond the RISC-V ESP32s.

## Where we are today

`vendor/espjtag/` (repo `awtoau/espjtag`) is a pure-Python,
pyusb-only client that drives the **RISC-V Debug Module** over the ESP32's
**built-in USB-Serial/JTAG** peripheral (`303a:1001`). The stack is:

```
USB (esp_usb_jtag vendor protocol, 4-bit TCK nibbles)
  -> JTAG TAP state machine (bitq.c-style)
    -> DTM/DMI scans (IR=DTMCS/DMI)
      -> RISC-V Debug Module: dmcontrol/dmstatus, abstract cmds (GPR/CSR),
         System Bus Access (memory)
```

It works on **C3 / C5 / C6 / H2** (all RISC-V): IDCODE, halt/resume, register
read/write, memory read/write — ~500 lines, no OpenOCD binary. See
`vendor/espjtag/jtag.py` and `vendor/espjtag/README.md`.

The two architectures the user *also* has — **Xtensa** (S2/S3) and **Nordic
nRF** (ARM Cortex-M) — need fundamentally different debug-module logic. This
doc summarises each protocol and recommends a backend strategy.

---

## 1. Xtensa (ESP32-S2 / ESP32-S3)

### Same wire, different brain

The S2 and S3 expose the **same `303a:1001` built-in USB-Serial/JTAG** peripheral
and the **same `esp_usb_jtag` USB transport** our espjtag already speaks — so the
USB layer, the 4-bit TCK nibble protocol, the `VEND_JTAG_SETDIV` clock, and the
JTAG TAP state machine are **reusable verbatim**. (ESP-IDF v6 docs list the S2 as
having built-in USB-Serial-JTAG; the S3 is well established. The S2 is
single-core Xtensa; the S3 is **dual-core** Xtensa, IDCODE `0x120034e5`,
Tensilica/part 0x2003.)

What changes is everything *above* the TAP. Xtensa does **not** have a RISC-V
Debug Module — there is no DTMCS/DMI, no `dmcontrol`/`dmstatus`, no abstract
commands, no System Bus Access. Instead it has the Tensilica **On-Chip Debug
(OCD) module**, a.k.a. the **Xtensa Debug Module (XDM)**, accessed via **Nexus**
address/data registers (NAR/NDR). Confirmed: OpenOCD implements this in a
completely separate target dir, `src/target/xtensa/` — `xtensa.c`,
`xtensa_chip.c`, `xtensa_debug_module.c/.h` — unrelated to `src/target/riscv/`.

### The TAP layer (NAR/NDR)

Xtensa JTAG uses **two TAP instructions plus a power-control pair** rather than
RISC-V's DTMCS/DMI. From OpenOCD `xtensa_debug_module.h` (TAP instruction
opcodes, verbatim):

| TAP instruction | IR value | DR width | Purpose |
|---|---|---|---|
| `TAPINS_PWRCTL`  | `0x08` | 8 bit  | power control (reset/wakeup domains) |
| `TAPINS_PWRSTAT` | `0x09` | 8 bit  | power status |
| `TAPINS_NARSEL`  | `0x1C` | addr 8 + data 32 | **the workhorse**: select a NAR register and read/write its 32-bit data |
| `TAPINS_IDCODE`  | (std)  | 32 bit | IDCODE |
| `TAPINS_BYPASS`  | (std)  | 1 bit  | bypass |

A NAR access (`TAPINS_NARSEL`) is a two-field scan: an **8-bit address byte**
(`reg_addr << 1 | read/write-bit`) followed by a **32-bit data** scan. This is
the Xtensa analogue of our `_dmi()` — instead of `(address<<34)|(data<<2)|op`,
it's an 8-bit NAR address + 32-bit NDR data, and you read back the previous
access's data (same "pipelined read" gotcha as RISC-V DMI).

### The NAR register map (XDM)

From OpenOCD `xtensa_debug_module.h` / Espressif `xtensa-debug-module.h`
(`NARADR_*` / `XDMREG_*`), the registers that matter for halt + read:

| NAR addr | Register | Use |
|---|---|---|
| `0x40` | OCDID | XDM ID |
| `0x42` / `0x43` | DCRCLR / DCRSET | clear/set bits in the Debug Control Register |
| `0x44` | **DSR** | Debug Status Register (EXECDONE, EXECEXCEPTION, EXECBUSY, **STOPPED**, COREWROTEDDR, DBGMODPOWERON …) |
| `0x45` | **DDR** | Debug Data Register — the host<->core data mailbox |
| `0x46` | DDREXEC | write DDR *and* trigger execution of DIR0 |
| `0x47` | **DIR0EXEC** | write instruction word to DIR0 **and execute it on the core** |
| `0x48`–`0x4f` | DIR0..DIR7 | Debug Instruction Registers (the instruction(s) to run) |
| (PWRCTL bits) | CORERESET/DEBUGRESET/COREWAKEUP/DEBUGWAKEUP/MEMWAKEUP | power-domain control |

DCR bits (from `OCDDCR_*`): `ENABLEOCD` (bit0), **`DEBUGINTERRUPT`** (bit1 — the
halt request), `STEPREQUEST` (bit3), plus BREAKINEN/BREAKOUTEN/RUNSTALLINEN.

### Halt + read sequence (matches how we documented the RISC-V DM)

Unlike RISC-V — where the DM has dedicated `dmcontrol`/abstract-command/SBA
hardware to do reg and memory access *for* you — **Xtensa debug works by feeding
machine-code instructions to the halted core** and shuttling data through the
single DDR mailbox. The CPU executes them out of DIR0 in a special debug context.
The pattern (per OpenOCD `xtensa.c`):

1. **Power up the debug domain.** `TAPINS_PWRCTL` scan, set
   `COREWAKEUP|MEMWAKEUP|DEBUGWAKEUP` (and clear `CORERESET`); poll
   `TAPINS_PWRSTAT` for `COREDOMAINON|DEBUGDOMAINON`.
2. **Enable OCD + halt.** NAR-write `DCRSET` with `ENABLEOCD`, then NAR-write
   `DCRSET` with `DEBUGINTERRUPT` (the equivalent of RISC-V `haltreq`). Poll
   `DSR` (NAR `0x44`) until **`STOPPED`** is set. On the dual-core S3, each core
   is a separate TAP; halting one debug-interrupts both (Espressif wires
   cross-core debug break), and you select a core by which TAP you scan.
3. **Read an AR (address/general) register** `aN`: there is no "read register"
   command. You **execute `WSR.DDR aN`** — i.e. write the instruction encoding
   for "move `aN` into the DDR special register" into **DIR0EXEC** (NAR `0x47`),
   which runs it on the halted core; the core writes `aN` into DDR; then you
   **NAR-read DDR** (`0x45`) over JTAG to get the value. Reading other special
   regs is `RSR.<sr> a0; WSR.DDR a0` (clobber+restore a scratch AR). OpenOCD
   pipelines a whole batch of these in `xtensa_fetch_all_regs()` and reads the
   DDRs back in one scan train.
4. **Read a memory word**: load the address into a scratch AR (via DDR), then
   execute `LDDR32P` (load-from-`[aN]`-into-DDR, with post-increment —
   `XT_INS_LDDR32P`, encoding `0x0070E0` LE) and NAR-read DDR. Auto-increment
   makes block reads a tight execute/read loop. Writes use `SDDR32P` (store).
5. **Resume**: execute `RFDO`/`RFDD` (Return From Debug Operation) and/or clear
   `DEBUGINTERRUPT` via `DCRCLR`.

So the conceptual mapping vs. our RISC-V client:

| RISC-V DM (espjtag today) | Xtensa OCD equivalent |
|---|---|
| DTMCS/DMI scan | NAR (`TAPINS_NARSEL`) 8-bit addr + 32-bit data |
| `dmcontrol.haltreq` | `DCR.DEBUGINTERRUPT` (via DCRSET), poll `DSR.STOPPED` |
| abstract cmd → GPR/CSR | **execute `WSR.DDR`/`RSR` via DIR0EXEC**, read DDR |
| System Bus Access (memory) | **execute `LDDR32P`/`SDDR32P` via DIR0EXEC**, read DDR |
| `ndmreset` | PWRCTL `CORERESET` |

### Effort to add Xtensa to espjtag

Roughly **1.5–2× the RISC-V code**, and conceptually trickier (you author
machine-code stubs, not register pokes). Concretely:

- **Reuse as-is:** the entire USB transport, nibble protocol, SETDIV, and the
  TAP state machine (~250 lines) — Xtensa rides the identical `esp_usb_jtag`
  wire.
- **New (~300–500 lines):** the `TAPINS_PWRCTL/PWRSTAT/NARSEL` scans, the NAR
  register read/write helper (with the pipelined-read handling), the
  power-up + `DEBUGINTERRUPT` halt, and — the fiddly part — a small table of
  hand-encoded Xtensa instructions (`WSR.DDR`, `RSR`, `LDDR32P`, `SDDR32P`,
  `RFDO`) fed through DIR0EXEC, plus scratch-AR save/restore so you don't corrupt
  the halted program's state. Memory read = an execute-then-read-DDR loop.
- **Endianness/windowing caveats:** Xtensa instruction encodings are
  endianness-sensitive (the `XT_INS_*` macros have BE/LE forms), and AR access
  goes through the register window — both are extra correctness footguns that
  the RISC-V port simply doesn't have.

**Recommendation for Xtensa: do not hand-roll it unless there's a specific need
for the no-dependency story on S2/S3.** It's doable and the transport is free,
but the instruction-injection model is materially more error-prone than RISC-V's
DM. For S2/S3 debug, prefer an existing tool (OpenOCD or **probe-rs**, which
already drives S2/S3 over this exact built-in USB-JTAG — see §3). Keep espjtag's
Xtensa support as a *roadmap/learning* item, not a blocker.

---

## 2. Nordic nRF (nRF52 / nRF53 / nRF91) — ARM Cortex-M

### Completely different stack — nothing from espjtag transfers

Nordic parts are **ARM Cortex-M** (nRF52 = M4F, nRF5340 = M33 app + M33 net,
nRF9160 = M33). They are debugged via **SWD** (or JTAG) using ARM's
**CoreSight / Debug Access Port (DAP)** architecture. There is **no ESP USB-JTAG
peripheral** and no Espressif transport — you need an external (or on-board) **DAP
probe**. The stack is:

```
USB (CMSIS-DAP: HID or bulk, DAP_Connect/DAP_Transfer commands)
  -> probe firmware drives SWD (2-wire: SWDIO/SWCLK)
    -> ARM DAP: SW-DP (debug port) + AHB-AP (access port)
      -> AHB-AP gives memory-mapped access to the whole Cortex-M bus, incl.
         the Debug Control Block at 0xE000EDF0+
        -> DHCSR halt, DCRSR/DCRDR register access
```

### The protocol layers

- **CMSIS-DAP** (host↔probe, over USB): standardised command set. Key commands
  `DAP_Connect` (0x02, select SWD/JTAG), `DAP_Transfer` (0x05, the workhorse —
  batched DP/AP register read/writes). Transport is USB **HID** (v1) or USB
  **bulk** (v2, faster); both are driverless. This is the layer a host library
  speaks.
- **SWD** (probe↔target): 2-pin serial. Host library never bit-bangs this — the
  probe firmware does, driven by `DAP_Transfer`.
- **ARM DAP**: SW-DP (`IDCODE`, `CTRL/STAT` — powers up debug/system domains via
  `CDBGPWRUPREQ`/`CSYSPWRUPREQ`) + **AHB-AP** (`CSW`/`TAR`/`DRW` — TAR=address,
  DRW=data → reads/writes any address on the Cortex-M AHB bus).
- **Cortex-M debug** (memory-mapped, reached through AHB-AP):
  - **DHCSR** `0xE000EDF0`: write `DBGKEY(0xA05F)|C_DEBUGEN|C_HALT` to halt;
    read back `S_HALT`. (Mirrors RISC-V `haltreq`→`allhalted`.)
  - **DCRSR** `0xE000EDF4` + **DCRDR** `0xE000EDF8`: select a core register and
    transfer it through DCRDR (the GPR/SP/PC/xPSR read-write path).
  - Memory = just AHB-AP TAR/DRW to the address — no special "SBA"; the AP *is*
    the bus master.
  - **DEMCR** `0xE000EDFC`: `VC_CORERESET` (halt-on-reset), and `SYSRESETREQ`
    in AIRCR for reset.

### Do Nordic boards expose a built-in probe like the ESP USB-JTAG?

**Yes — the Nordic DKs have an on-board SEGGER J-Link OB ("Interface MCU").**
- **nRF52840-DK / nRF52-DK / nRF5340-DK**: on-board J-Link OB on a secondary MCU
  wired to the target's SWD pins, exposed over the DK's USB. It speaks **J-Link**
  natively and can also be flashed/configured to expose **CMSIS-DAP / DAPLink**.
  So a single USB cable to the DK is enough — no external probe needed.
- **Bare modules / dongles** (e.g. nRF52840 Dongle, custom boards): **no on-board
  probe** — you need an external **CMSIS-DAP** probe, a **SEGGER J-Link**, or
  another DK used as a probe. (Makerdiary Pitaya-Link, the official
  nRF52840-DK-as-probe, a Raspberry Pi Pico running picoprobe/CMSIS-DAP, etc.)

This is the big practical difference from ESP: ESP RISC-V/Xtensa debug needs only
the chip's own USB; Nordic debug needs a **DAP probe** in the path (on-board on a
DK, external on a bare module).

### Recommendation for Nordic: use pyOCD — don't reimplement

**pyOCD is exactly this stack in pure Python** (Arm-maintained): CMSIS-DAP →
SWD → ARM DAP → AHB-AP → Cortex-M halt/registers/memory/flash. It is the Python
analogue of what espjtag is for ESP RISC-V — and it already exists, is mature,
and is the one ARM ships.

- **nRF coverage**: nRF52 family is **built-in** (e.g. `nRF52840_xxAA`,
  `nRF52833`, and the rest of the nRF52 line ship as built-in targets). nRF5340
  (both app + net cores) and nRF9160 are supported via a **CMSIS Device Family
  Pack** — `pyocd pack install nrf53` / `nrf91`, then
  `pyocd flash -t nrf5340_xxaa …`. Confirmed working on nRF5340 dual-core with
  CMSIS-DAP per Nordic DevZone.
- **Probe support**: CMSIS-DAP (HID + v2 bulk) and ST-Link natively; J-Link via
  its CMSIS-DAP mode / DAPLink. So a Nordic DK's on-board J-Link OB (in
  CMSIS-DAP mode) or any CMSIS-DAP probe → pyOCD just works.
- **What we'd do**: `pip install pyocd`, optionally `pyocd pack install nrf53`,
  then drive it via its Python API (connect, halt, read_core_register,
  read_memory) — the same shape as espjtag, so the GUI's debug abstraction maps
  onto it cleanly. **We reuse pyOCD; we do not write a CMSIS-DAP/SWD/DAP client.**

(A from-scratch pure-Python CMSIS-DAP+DAP+Cortex-M client is feasible — the
DHCSR/DCRSR/AHB-AP sequence above is simpler than Xtensa's instruction injection
— but pyOCD already *is* that, with flash algorithms and CMSIS-Pack target data
we'd otherwise have to recreate. No reason to.)

---

## 3. probe-rs — the one tool that already spans all three

**probe-rs** (Rust) is the notable unifier. It debugs **ARM, RISC-V *and*
Xtensa** over SWD/JTAG, and crucially it has a built-in **EspJtag** driver that
speaks the *same* `303a:1001` `esp_usb_jtag` transport our espjtag uses — so it
drives ESP **RISC-V** (C3/C6/H2/…) **and ESP Xtensa (S2/S3)** over the chip's
built-in USB with no external hardware, and ARM Nordic via a CMSIS-DAP/J-Link
probe.

- ESP coverage: "USB-JTAG-SERIAL peripheral … ESP32-C6, ESP32-H2, ESP32-S3 and
  ESP32-C3" usable "without any external hardware"; probe-rs explicitly added
  **Xtensa support sufficient to connect, inspect and flash the ESP32-S2 and
  ESP32-S3** (Xtensa tracking issue #2001). It detects the ESP TAP as
  "ESP JTAG -- 303a:1001".
- ARM coverage: hundreds of Cortex-M targets including the nRF52/53 line (the
  same CMSIS-Pack-derived target database family pyOCD uses).
- Downside vs. pyOCD/espjtag: it's a **Rust binary**, not a Python library — less
  "drop a module in and import it," more "shell out to / link a native tool."

---

## 4. Unifying-backend recommendation

The GUI/debugger (separate task) leans toward **talking a debug protocol over the
network**, so the backend is pluggable behind a socket. Given that:

### Recommended: a thin per-architecture backend set behind one network protocol

| Architecture | Parts the user has | Backend | Why |
|---|---|---|---|
| **ESP RISC-V** | C3, C5, C6, H2 | **espjtag** (ours) | Already done, pure-Python, no binary, no OpenOCD. The lightweight default. |
| **Nordic ARM** | nRF52/53/91 | **pyOCD** | Mature, pure-Python, Arm-maintained; CMSIS-DAP/J-Link; built-in nRF52 + packs for 53/91. Don't reinvent. |
| **ESP Xtensa** | S2, S3 | **OpenOCD or probe-rs** (not hand-rolled) | Instruction-injection debug is error-prone; both tools already do S2/S3 over the same built-in USB-JTAG. |

Rationale: each piece is either *already ours and free* (espjtag for ESP RISC-V)
or *a mature pure-Python/standard tool we'd only be re-creating* (pyOCD for
Nordic). The only genuinely new code would be Xtensa — and that's exactly the
case where reusing an existing tool wins most. All of them sit behind the GUI's
network debug protocol, so the GUI doesn't care which is underneath.

### Alternative A — OpenOCD-for-everything

OpenOCD supports **all three**: ESP RISC-V (`board/esp32c*-builtin.cfg`), ESP
Xtensa (`src/target/xtensa/`, `board/esp32s3-builtin.cfg`), and ARM/Nordic via
CMSIS-DAP/J-Link. It already exposes a **network protocol** (GDB-remote on :3333,
plus the Tcl/RPC port) — a natural fit for the GUI's "debug over the network"
design. We already ship and depend on the **Espressif-patched OpenOCD** for the
C6 post-flash boot (`docs/C6-USJ-RESET.md`), so it's in the toolchain anyway.
- **Pro**: one backend, one config language, covers every chip the user owns,
  network-native (GDB RSP), battle-tested.
- **Con**: a big native binary + config tree; needs the *Espressif* build for ESP
  (vanilla OpenOCD has no ESP support); heavier than espjtag for the common
  ESP-RISC-V case.

### Alternative B — probe-rs-for-everything

One tool, all three architectures, ESP via built-in USB-JTAG with no external
hardware (§3). Modern, actively developed.
- **Pro**: genuinely covers ARM + RISC-V + Xtensa in a single tool; great ESP
  built-in-USB story.
- **Con**: a Rust binary (not a Python lib); the GUI would shell out / use its
  DAP server rather than `import` it. Xtensa support is newer/less battle-tested
  than OpenOCD's.

### Verdict

- **For the GUI as designed (debug over a network protocol): make the backend
  pluggable, and use OpenOCD as the universal fallback** behind that network
  protocol (it speaks GDB-remote natively and covers all three architectures,
  and we already carry the Espressif build). 
- **Keep espjtag as the lightweight, no-OpenOCD option for ESP-RISC-V
  specifically** — it's the "just a USB cable and pyusb" path and stays our
  default for C3/C5/C6/H2.
- **Use pyOCD for Nordic** if/when we want a pure-Python Nordic path instead of
  routing Nordic through OpenOCD (both work; pyOCD is the lighter, Python-native
  choice and maps onto the same API shape as espjtag).
- **Do not hand-roll Xtensa** in espjtag for now: route S2/S3 through OpenOCD (or
  probe-rs). Revisit only if a no-dependency S2/S3 client becomes worth the
  instruction-injection complexity.
- **probe-rs is the one to watch** as a future single-binary unifier — if its
  Xtensa support matures, it could replace the OpenOCD-fallback role with a
  cleaner ESP-built-in-USB story.

---

## 5. Support matrix

| Chip(s) | Arch | Debug module | Transport / probe | espjtag (ours) | pyOCD | OpenOCD (esp) | probe-rs |
|---|---|---|---|---|---|---|---|
| C3 / C5 / C6 / H2 | RISC-V | RISC-V Debug Module (DTM/DMI) | built-in USB-JTAG `303a:1001` | **yes (built)** | no (ARM only) | yes | yes |
| S2 (single core) | Xtensa LX7 | Xtensa OCD / XDM (NAR + DIR0EXEC) | built-in USB-JTAG `303a:1001` | no (could add — [#9](https://github.com/awtoau/espjtag/issues/9)) | no | yes | yes |
| S3 (dual core) | Xtensa LX7 | Xtensa OCD / XDM (NAR + DIR0EXEC) | built-in USB-JTAG `303a:1001` | no (could add — [#9](https://github.com/awtoau/espjtag/issues/9)) | no | yes | yes |
| nRF52 (e.g. 840) | Cortex-M4F | ARM CoreSight DAP + AHB-AP | SWD via CMSIS-DAP / J-Link OB | no | **yes (built-in)** | yes (CMSIS-DAP) | yes |
| nRF5340 | Cortex-M33 ×2 | ARM CoreSight DAP + AHB-AP | SWD via CMSIS-DAP / J-Link OB | no | yes (`pack install nrf53`) | yes | yes |
| nRF9160 | Cortex-M33 | ARM CoreSight DAP + AHB-AP | SWD via CMSIS-DAP / J-Link OB | no | yes (`pack install nrf91`) | yes | yes |

Probe needed: ESP parts = **the chip's own USB cable** (built-in USB-JTAG).
Nordic parts = **a DAP probe** — on-board J-Link OB on the DKs, or an external
CMSIS-DAP/J-Link for bare modules/dongles.

---

## Sources

**Xtensa OCD / XDM:**
- OpenOCD Xtensa target source — `src/target/xtensa/` (`xtensa.c`,
  `xtensa_chip.c`, `xtensa_debug_module.c/.h`):
  <https://github.com/openocd-org/openocd/blob/master/src/target/xtensa/xtensa_debug_module.h>
  (TAPINS_PWRCTL=0x08, PWRSTAT=0x09, NARSEL=0x1C; DCR/DSR/DDR/DIR0EXEC defs).
  Doxygen: <https://openocd.org/doc/doxygen/html/xtensa_8c_source.html>,
  <https://openocd.org/doc/doxygen/html/xtensa__debug__module_8c_source.html>
- Espressif `xtensa-debug-module.h` (NARADR_* map: OCDID 0x40, DCRCLR 0x42,
  DCRSET 0x43, DSR 0x44, DDR 0x45, DDREXEC 0x46, DIR0EXEC 0x47, DIR0..7 0x48–4f):
  <https://github.com/espressif/esp-idf/blob/master/components/xtensa/include/xtensa-debug-module.h>
- Xtensa ISA (instruction encodings RSR/WSR/LDDR32P/SDDR32P/RFDO):
  <https://dl.espressif.com/github_assets/espressif/xtensa-isa-doc/releases/download/latest/Xtensa.pdf>
- Lauterbach Xtensa debugger manual (XDM via JTAG / DAP, DOSR/DSR/DDR):
  <https://www2.lauterbach.com/pdf/debugger_xtensa.pdf>
- ESP32-S3 dual-core / built-in JTAG (two TAPs, IDCODE 0x120034e5, synchronised
  halt/resume):
  <https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-guides/jtag-debugging/index.html>
- ESP32-S2 (single-core Xtensa, built-in USB-Serial-JTAG, 303a:1001):
  <https://docs.espressif.com/projects/esp-idf/en/stable/esp32s2/api-guides/jtag-debugging/index.html>

**ARM CoreSight / CMSIS-DAP / pyOCD (Nordic):**
- pyOCD built-in targets (nRF52840/nRF52833 built-in):
  <https://pyocd.io/docs/builtin-targets.html> ,
  <https://github.com/pyocd/pyOCD/blob/main/pyocd/target/builtin/target_nRF52840_xxAA.py>
- pyOCD target support / CMSIS-Packs (nrf53, nrf91 via `pyocd pack install`):
  <https://pyocd.io/docs/target_support.html>
- pyOCD Cortex-M core (DHCSR/DCRSR/DCRDR halt + register access) and AP:
  <https://github.com/pyocd/pyOCD/blob/main/pyocd/coresight/cortex_m.py> ,
  <https://github.com/pyocd/pyOCD/blob/main/pyocd/coresight/ap.py>
- nRF5340 dual-core flashing with pyOCD + CMSIS-DAP (Nordic DevZone):
  <https://devzone.nordicsemi.com/f/nordic-q-a/108628/flashing-nrf5340-using-pyocd-works-with-both-cores-with-cmsis-dap>
- CMSIS-DAP protocol (DAP_Connect 0x02 / DAP_Transfer 0x05, HID + bulk):
  <https://arm-software.github.io/CMSIS_5/DAP/html/index.html> ,
  <https://os.mbed.com/handbook/CMSIS-DAP>
- ARM Cortex-M debug-interface deep dive (DP/AP/AHB-AP, DHCSR/DCRSR/DCRDR):
  <https://interrupt.memfault.com/blog/a-deep-dive-into-arm-cortex-m-debug-interfaces>
- Nordic DK on-board J-Link OB / CMSIS-DAP (DAPLink) mode:
  <https://devzone.nordicsemi.com/f/nordic-q-a/46383/nrf52840-dk-onboard-segger-j-link> ,
  <https://wiki.makerdiary.com/nrf52840-mdk-usb-dongle/programming/daplink/>

**probe-rs (unifier):**
- probe-rs (ARM + RISC-V + Xtensa via SWD/JTAG; EspJtag built-in USB-JTAG driver):
  <https://probe.rs/> , <https://github.com/probe-rs/probe-rs>
- probe-rs Xtensa tracking issue (S2/S3 connect/inspect/flash):
  <https://github.com/probe-rs/probe-rs/issues/2001>
- probe-rs on ESP (USB-JTAG-SERIAL, no external hardware):
  <https://docs.espressif.com/projects/rust/book/getting-started/tooling/probe-rs.html>

**Our existing RISC-V client (baseline):**
- `vendor/espjtag/espjtag/{transport,debug,reset}.py`, `vendor/espjtag/README.md`
  (esp_usb_jtag transport + RISC-V DM);
  `docs/C6-USJ-RESET.md` (we already ship Espressif-patched OpenOCD).
