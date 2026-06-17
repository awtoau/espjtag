# espjtag

**One pure-Python tool for the ESP32 built-in USB-JTAG — debug, flash, identify.
Faster than probe-rs and OpenOCD on the same wire. No OpenOCD, no esptool, no
adapter — just the USB cable.**

An [awto.au](https://awto.au) project.

> **Built on the shoulders of [OpenOCD](https://github.com/espressif/openocd-esp32)
> and [probe-rs](https://github.com/probe-rs/probe-rs) — thank you for your
> fantastic work.** The protocol logic here is a faithful 1:1 reimplementation of
> theirs in pure Python, transcribed verbatim. Where our code is correct, it's
> because theirs is. The rules we port by:
> [`docs/FAITHFUL-REIMPLEMENTATION.md`](docs/FAITHFUL-REIMPLEMENTATION.md).

**The speed** (measured fleet-wide, fair warm-transport comparison, ESP32-C6 —
`docs/JTAG-BENCHMARK-ANALYSIS.md` + the recorded run DB):

| bulk memory (µs/word) | **espjtag** | probe-rs | OpenOCD |
|---|---|---|---|
| read, 1024-word burst | **21** | 46 | 97 |
| write, 1024-word burst | **30** | ~37 | 27 |

| flash a 64 KiB A→B update (2 sectors changed) | wall clock |
|---|---|
| **espjtag incremental** (resident RAM stub, on-chip CRC diff, verify included) | **~280 ms** |
| esptool incremental (serial, device-diff fork) | ~560 ms |
| esptool full (serial) | ~1.1 s |
| OpenOCD / probe-rs full (JTAG) | 1.7–1.9 s |

**The single tool**: halt/resume, registers, memory, reset, **incremental
flash** (on-chip CRC-32 diff → write only changed sectors → verify), 64 KiB
block erase, NOR-aware in-place writes, and flash die identification (JEDEC
RDID + SFDP over bare SPI1 registers) — one `pip install pyusb` away, in pure
Python. OpenOCD-class debug + esptool-class flashing + flashrom-class identify,
without installing any of them.

The ESP32-C3/C5/C6/H2 expose a USB-Serial/**JTAG** peripheral on their native USB
(`303a:1001`). `espjtag` speaks the Espressif `esp_usb_jtag` USB protocol directly
(via [pyusb]) and drives the RISC-V Debug Module.

## Why

`esptool` flashes over the serial path; OpenOCD does real debug but needs the
Espressif OpenOCD build and its config tree. For scripting, CI, or just
understanding what the silicon does, a small dependency-free Python client is
handy. `espjtag` is that — it was extracted from a bench tooling project where it
replaced shelling out to OpenOCD for register/memory access and chip reset, and
then out-ran the tools it replaced (the transport story: one USB round-trip per
batch, OUT-only write streams, full-rate TCK — `docs/ESPJTAG-STORY.md`).

## Install

```sh
pip install pyusb       # the only dependency
# then drop the espjtag/ package on your path, or:
pip install -e .
```

You need permission to access the USB device (a udev rule for `303a:1001`, or run
as root).

## Use

```python
from espjtag import EspUsbJtag

j = EspUsbJtag()                      # first 303a:1001 on the bus
                                      # or EspUsbJtag("1-1.3.1.3.4") to pin a port
print(hex(j.read_idcode()))           # 0x0000dc25 on a C6

j.examine()
j.halt()                              # stop the core
print(hex(j.read_register(0x1000+1))) # x1 / ra
print(hex(j.read_register(0x7b1)))    # dpc — the PC where it halted
print(hex(j.read_mem32(0x42000000)))  # memory-mapped flash
j.write_mem32(0x40810000, 0xCAFEBABE) # write SRAM via System Bus Access
j.resume()
```

CLI quick checks:

```sh
python -m espjtag                 # dump IDCODE + DM registers (read-only)
python -m espjtag --selftest      # verify the JTAG stack (C6)
```

## What works

| Capability | Status |
|---|---|
| esp_usb_jtag USB transport (vendor iface, bulk eps, SETDIV) | ✅ |
| JTAG TAP state machine (IDCODE, DTMCS) | ✅ |
| DMI read/write (dmcontrol, dmstatus, hartinfo) | ✅ |
| Halt / resume / examine | ✅ |
| GPR + CSR read/write (abstract command) | ✅ |
| Memory read/write (System Bus Access) | ✅ |
| `reset_run()` — ndmreset, reboot a running core | ✅ |
| Boot a chip out of post-flash ROM download (`reset_run_from_rom()`) | ⚠️ Linux: needs bench re-verify after the cross-platform refactor ([#13](docs/CROSS-PLATFORM-USB.md)); macOS no-op |
| Cross-platform USB reset (Win/Linux/mac) | ✅ pyusb `dev.reset()` — see [docs/CROSS-PLATFORM-USB.md](docs/CROSS-PLATFORM-USB.md) |
| Flash programming over JTAG (full + **incremental**, block erase, in-place writes, pipelined staging) | ✅ fastest flasher on our bench (~280 ms for a 64 KiB 2-sector update via the resident RAM stub, verify incl.) |
| Flash die identify (JEDEC RDID + SFDP via SPI1 registers) | ✅ `python -m espjtag <usb> --info` |

## MCP server (debug from an AI client)

`espjtag` ships an [MCP](https://modelcontextprotocol.io) server so an MCP client
(Claude Code, the planned VS Code extension
[#15](https://github.com/awtoau/espjtag/issues/15), …) can drive the debugger as
tools — list probes, read IDCODE/memory/registers, halt/resume, and reset an
ESP32 over JTAG:

```sh
pip install mcp                 # the official Python SDK (extra dependency)
python -m espjtag.mcp           # serve MCP over stdio

# register with Claude Code:
claude mcp add espjtag -- python -m espjtag.mcp
```

Read-only tools (`list_probes`, `idcode`, `diag`, `read_memory`, `probe`) don't
disturb the running firmware; mutating tools (`halt`, `resume`, `read_register`,
`write_register`, `write_memory`, `reset_run`, `reset_from_rom`) pause or reset
the target and are annotated accordingly. With many boards on the bus, every tool
pins a unit by `usb_path` or `serial`. Full tool list, design notes, and client
config: **[docs/MCP-SERVER.md](docs/MCP-SERVER.md)**.

## Scope

RISC-V parts only (C3/C5/C6/H2). The Xtensa parts (S2/S3) use the **same USB
transport** but a different debug module (Xtensa OCD/TRAX, not the RISC-V Debug
Module), so the register-level API here doesn't apply to them.

## Credits & provenance

The `esp_usb_jtag` USB protocol and the JTAG/RISC-V debug details were ported from
[openocd-esp32] (`src/jtag/drivers/esp_usb_jtag.c`, `bitq.c`, GPL-2.0-or-later)
and the [RISC-V External Debug Support][riscv-debug] register map via openocd-esp32's
`src/target/riscv/debug_defines.h` (BSD-2-Clause OR CC-BY-4.0). Not affiliated with
Espressif, OpenOCD, or RISC-V International.

See **[ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md)** for the full per-file provenance,
the pinned upstream commit, and a license-compatibility note (Apache-2.0 vs. the
GPL-2.0 / BSD sources — what was ported and why it's the mainstream reading that
bare interface constants are usable). The exact upstream pin and the symbols we
depend on are in [`upstream.lock`](upstream.lock); `python3 scripts/check_upstream.py`
gives a GO/NO-GO if upstream drifts.

## License

Apache-2.0 (see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE)). Ported numeric
constants are credited per the upstream licenses in
[ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md).

[pyusb]: https://github.com/pyusb/pyusb
[openocd-esp32]: https://github.com/espressif/openocd-esp32
[riscv-debug]: https://github.com/riscv/riscv-debug-spec
