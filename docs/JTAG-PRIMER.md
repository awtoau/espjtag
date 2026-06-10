# JTAG, from the bottom up

How espjtag actually works, built up from the wire. The short answer to *"is it
just one long set of bits in the end?"* — **yes.** Everything is a shift register
clocked one bit per tick; all the structure is layered on top.

## 1. The wire: 1 bit per clock

JTAG is, physically, a few signals:

| Signal | Meaning |
|---|---|
| **TCK** | clock — everything happens on its edges |
| **TMS** | mode-select — one bit/clock steers the TAP state machine |
| **TDI** | data **in** (bits you send to the chip) |
| **TDO** | data **out** (bits the chip returns) |

On each TCK tick: one bit goes in on TDI, one bit comes out on TDO, and TMS
decides the "mode." A scan of N bits = N ticks. **It is a shift register.**

In espjtag there are no physical pins — the ESP32 built-in USB-JTAG (`esp_usb_jtag`)
encodes each tick as a 4-bit USB command nibble:

```
CMD_CLK = 0b0 <cap> <tms> <tdi>     # one TCK; cap=1 captures TDO to the IN endpoint
```

Same idea: one clock, one bit each way, plus a "capture this TDO" flag.

## 2. The one piece of structure: the TAP state machine

You can't just shift bits — the chip must know *which* register you're shifting
and *when* the shift starts/stops. TMS drives a fixed 16-state machine (the TAP).
The states that matter:

```
                    ┌─────────────── TMS=1 ──────────────┐
Run-Test/Idle ─1─► Select-DR ─0─► Capture-DR ─0─► Shift-DR ─1─► Exit1-DR ─1─► Update-DR ─► (Idle)
                    │                                  ▲   │
                    1                                  └─0─┘  (stay & shift)
                    ▼
                  Select-IR ─0─► Capture-IR ─0─► Shift-IR ─1─► Exit1-IR ─1─► Update-IR ─► (Idle)
```

You **walk this machine with TMS bits only** (TDI/TDO don't matter during the
walk). Once in **Shift-DR** or **Shift-IR**, each clock shifts one real data bit.
That's exactly what `transport.py` does: `_goto(SHIFT_DR)` walks there with TMS,
then a loop shifts the data bits (last bit with TMS=1 to exit). espjtag tracks the
current TAP state explicitly (like OpenOCD's `bitq.c`) so a scan is correct from
any starting state.

## 3. Two registers you shift through

- **IR (Instruction Register)** — short (5 bits here). Shift in an opcode to pick
  *which* data register comes next. On these RISC-V parts:
  `IDCODE=0x01`, `DTMCS=0x10`, `DMI=0x11`.
- **DR (Data Register)** — whatever the current IR selected. 32 bits for IDCODE,
  41 bits for a DMI access. Shift your data through; the old contents shift out on
  TDO.

So the whole protocol is: **shift an IR to pick a register, shift a DR to
read/write it, repeat.**

## 4. From bit-chains to "read a register / memory"

The bits aren't magic — they're a stack:

| Layer | What it is | espjtag file |
|---|---|---|
| **Wire** | 1 bit per TCK (the long bit-chain) | `transport._clk` |
| **TAP** | TMS walks the state machine; in Shift-* you shift N bits | `transport._scan` / `_goto` |
| **DMI** | a 41-bit DR scan = `address<<34 \| data<<2 \| op` → read/write a Debug Module register | `transport.dmi_read/_write` |
| **Debug Module** | dmcontrol/dmstatus/abstract-command/System-Bus registers → halt the CPU, read GPRs/CSRs, read/write memory | `debug.py` |

So "read memory at 0x42000000" is really: shift IR=DMI; shift a 41-bit DR that
writes the System-Bus address register; shift another that reads the data register
— **just bit-chains**, each poking a register the silicon interprets as a command.
The `ndmreset` reboot is the same: a DMI write of `dmcontrol` with the ndmreset
bit set.

## 5. Gotchas that fall straight out of "it's one long bit-chain"

- **Off-by-one capture.** TDO for bit N can land one clock from where you expect
  (the Capture→Shift transition). espjtag captures all N shift clocks with the
  last bit on the Exit1 (TMS=1) clock — verified against the C6 IDCODE
  (`0x0000dc25`).
- **IN-endpoint desync.** Because captured TDO is a byte stream returned per scan,
  stale/padding bytes from a previous scan desync the next read. espjtag drains
  the IN endpoint before each op (the bug where only the first read per session
  worked).
- **DMI is 2-phase for reads.** A READ scan returns the *previous* access's data,
  so you scan READ@addr then a NOP to collect it — both in one TAP session.
- **Idle cycles.** DTMCS advertises an `idle` count; you must clock that many
  Run-Test/Idle cycles after a DMI op before the result is ready.

## TL;DR

One primitive — *shift these bits through this register* — repeated with different
bit patterns is the entire debugger. IDCODE, DMI, halt, memory read, ndmreset:
all the same shift, different bits. That's why it fits in a few hundred lines.

See also: openocd-esp32 `src/jtag/drivers/esp_usb_jtag.c` + `bitq.c` (the
transport + scan model espjtag ports), and the RISC-V External Debug Support spec
(the DMI/Debug-Module layer).
