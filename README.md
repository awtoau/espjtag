<img src="assets/awto-logo.svg" alt="awto.au" width="200">

# espjtag

**Pure-Python RISC-V JTAG debugger for the ESP32 built-in USB-JTAG — no OpenOCD.**

An [awto.au](https://awto.au) project.

The ESP32-C3/C5/C6/H2 expose a USB-Serial/**JTAG** peripheral on their native USB
(`303a:1001`). `espjtag` speaks the Espressif `esp_usb_jtag` USB protocol directly
(via [pyusb]) and drives the RISC-V Debug Module — so you can halt the core, read
and write registers and memory, and reset the chip, all in a few hundred lines of
Python with no OpenOCD binary, no FTDI adapter, just the USB cable.

## Why

`esptool` flashes over the serial path; OpenOCD does real debug but needs the
Espressif OpenOCD build and its config tree. For scripting, CI, or just
understanding what the silicon does, a small dependency-free Python client is
handy. `espjtag` is that — it was extracted from a bench tooling project where it
replaced shelling out to OpenOCD for register/memory access and chip reset.

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
| Flash programming over JTAG | 🚧 roadmap (ROM `esp_rom_spiflash_*` call) |

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
