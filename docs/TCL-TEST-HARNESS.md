# The espjtag Tcl test harness

A pure-Python Tcl interpreter (`scripts/ocd_tcl_bridge.py` + `scripts/mini_jimtcl.py`)
that runs OpenOCD's *verbatim* config Tcl, with the hardware leaf commands
(`mww`/`mdw`/`riscv`/...) implemented on espjtag's own transport — **no OpenOCD
process**. On top of that base it adds commands to **test** espjtag itself:
correctness, behaviour, and performance, runnable **with or without a chip**.

This is the same architecture the established debuggers use:
- **OpenOCD** — a Jim Tcl interpreter + a `dummy` adapter + a `testee` target, with
  tests written as Tcl scripts (`testing/tcl_commands/`).
- **probe-rs** — a pure plan (`FlashLayout`, golden-tested) + a thin hardware apply
  (`Flasher`) + a `FakeProbe` dry-run for no-hardware tests.

espjtag has all of these: a pure plan (`plan_load`), a Tcl harness (this), a mock
transport (`MockXtensaXDM`), performance instrumentation, and on-target runs.

## Why Tcl?

Tcl (Tool Command Language) is a tiny embeddable scripting language. OpenOCD
embeds **Jim Tcl** so that per-chip logic — reset sequences, flash procedures,
board configs (`esp32s3.cfg`) — ships as *scripts*, not recompiled C. That means
espjtag's bridge can run Espressif's chip configs **unmodified**, and our tests
are scripts too. You write a test as a sequence of commands with `assert_*`
checks; no rebuild, no separate test framework.

## Concepts (spelled out)

- **Xtensa Debug Module** — the on-chip debug hardware inside an Xtensa core
  (ESP32 / ESP32-S3). The Xtensa equivalent of the RISC-V "Debug Module" on the
  C3/C5/C6. espjtag drives it via the `XtensaXDM` class (`espjtag/xtensa.py`).
  You halt/resume, read/write memory and registers, and inject instructions
  through it over JTAG.
- **Flasher stub** — a small prebuilt program (from OpenOCD-esp32) loaded into
  target RAM and run, to flash/erase/inspect flash faster than per-call ROM
  routines. espjtag loads the *same* stub OpenOCD does (`espjtag/xtensa_stubs.py`,
  extracted from source; `espjtag/xtensa_flasher.py`, the loader/runner).
- **Plan vs apply** — `plan_load(command)` is PURE: it computes the memory image
  (every `(address, bytes)` write) the load *would* do, touching no hardware.
  `load()` is the thin layer that applies the plan to the chip. Tests validate
  the plan deterministically; only genuine chip behaviour needs silicon.
- **Software model (mock)** — `MockXtensaXDM` records every memory/register
  operation and serves reads from an in-memory model, so the *whole* flasher
  operation-sequence runs with **no chip** (the OpenOCD `dummy` / probe-rs
  `FakeProbe` analogue). It does NOT emulate the core — `xtensa_mock_run` scripts
  the stub's return value; only "does the silicon execute the stub" needs an S3.

## Command naming convention

- **`<extension>_<noun>_<verb>`** for processor-specific commands — the name alone
  says which extension, what it acts on, and the action (e.g. `xtensa_core_halt`,
  `xtensa_stub_load`).
- **`noun_verb`** for generic test/performance commands (`assert_equal`,
  `time_mark`).
- **Register roles, not raw numbers** — commands take meaningful role names
  (`entry_point`, `stack_pointer`, `return_value`, `return_address`) instead of
  Xtensa a-register numbers (a8/a1/a2/a0). The output shows both.

## Running

```
# No hardware (pure + software-model tests):
python3 scripts/ocd_tcl_bridge.py --mock --tcl scripts/test_s3_mock.tcl

# On a target (full step test):
python3 scripts/ocd_tcl_bridge.py --usb 1-1.3.3.4 --tcl scripts/test_s3_stub.tcl

# Performance instrument:
python3 scripts/ocd_tcl_bridge.py --usb 1-1.3.3.4 --tcl scripts/perf_s3_stub.tcl
```

## Command reference

### Base (OpenOCD leaf commands, on espjtag's transport)
| Command | Hardware | Meaning |
|---|---|---|
| `mww <addr> <value>` | yes | memory write word |
| `mdw <addr> [n]` | yes | memory display word(s) → hex |
| `riscv dmi_read\|dmi_write ...` | yes | RISC-V Debug Module Interface access |
| `poll` | yes | read the Debug Module status |

### Xtensa stub flasher — pure (NO hardware)
| Command | Meaning |
|---|---|
| `xtensa_stub_plan_address <command> <name>` | a planned address by name: `entry`, `tramp_mapped_addr`, `stack_addr`, `dram_org`, `iram_org`, `trap_entry_addr` |
| `xtensa_stub_plan_writes <command>` | number of `(address, bytes)` writes the load would do |
| `xtensa_stub_plan_bytes <command> <write_index> <count>` | first `<count>` bytes (hex) of a planned write — golden-check the reversed code / normal data |
| `xtensa_plan_assert_no_overlap <command>` | PASS if no two planned writes overlap (auto-catches layout collisions) |

### Xtensa stub flasher — on a target (JTAG)
| Command | Meaning |
|---|---|
| `xtensa_core_halt` | power up + halt the Xtensa core; returns 1 if halted |
| `xtensa_stub_load <command>` | load a prebuilt stub into target RAM; returns `entry stack trampoline` addresses |
| `xtensa_stub_run [arg ...]` | run the loaded stub; returns its value (hex) or `TIMEOUT` |

### Xtensa stub flasher — software model (NO hardware)
| Command | Meaning |
|---|---|
| `xtensa_mock_load <command>` | load against the model (records writes); returns write count |
| `xtensa_mock_run <return_value> [arg ...]` | script the stub's return value, run against the model; returns what the flasher read back |
| `xtensa_memory_expect <address> <hex_bytes>` | assert the model RAM at `<address>` equals the golden bytes |
| `xtensa_register_expect <role> <value>` | assert the run set a register, by role: `entry_point`, `stack_pointer`, `return_value`, `return_address` |
| `xtensa_operation_count [kind]` | model operations, total or by kind (`write_mem`, `read_mem`, `set_ar`, `set_sr`, `nar_write`) |

### Generic test + performance
| Command | Meaning |
|---|---|
| `value_equal <a> <b>` | 1 if the two strings are equal, else 0 |
| `assert_equal <label> <got> <want>` | print PASS/FAIL; returns 1/0 |
| `assert_less_than <label> <got> <limit>` | PASS if `float(got) < limit` (a performance gate) |
| `time_mark <name>` | record a timestamp + transaction count for a span |
| `time_elapsed_ms <name>` | milliseconds since `time_mark <name>` |
| `jtag_transaction_count [name]` | total bridge transactions, or those since `time_mark <name>` |

## Example (no hardware)

```tcl
# the load LAYOUT is correct without touching a chip:
assert_equal "entry" [xtensa_stub_plan_address cmd_test1 entry] 0x4038c2c0
xtensa_plan_assert_no_overlap cmd_test1
# run the whole flasher against the software model:
xtensa_mock_load cmd_test1
xtensa_mock_run 0
xtensa_register_expect entry_point 0x4038c2c0   ;# the trampoline got the entry
```

## The test boundary (what is and isn't emulated)

| Layer | No-hardware? | What it validates |
|---|---|---|
| Host computation — layout, reversed bytes, addresses | ✅ `plan_load` + `xtensa_stub_plan_*` | "Do I compute the right image?" |
| Operation sequence — the write/register/resume order | ✅ `MockXtensaXDM` + `xtensa_mock_*` | "Do I issue the right operations?" |
| Chip behaviour — the core runs the stub, flash is written | ❌ needs a real S3 | "Does the silicon do it?" |

The third row is deliberately not emulated: that would be a full core emulator
(QEMU-scale), and even Espressif tests stub *behaviour* on real chips. The first
two rows — where transcription bugs hide — are fully testable without a chip.
