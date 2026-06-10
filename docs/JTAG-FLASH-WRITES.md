# High-speed flash writes over JTAG

How to program an ESP32's SPI flash through the JTAG debug link at high speed — the
design espjtag follows. **Status: DESIGN / ROADMAP (issue #3).** Today espjtag has
the building blocks (memory read/write via System Bus Access, batched bursts,
halt/resume, register + PC control) but does **not** yet program flash. This
documents how it works and how we'll build it, so it's clear what's proven vs
planned.

## The core idea: don't bit-bang flash over JTAG — run a loader ON the chip

There are two ways to write flash through a debug link:

1. **Naïve / slow:** drive the SPI-flash controller's MMIO registers one
   transaction at a time over JTAG (write-enable, page-program, poll-WIP, repeat),
   each a separate JTAG round-trip. At ~hundreds of µs per round-trip over USB
   Full-Speed (1 ms frames), and ~hundreds of round-trips per flash page, this is
   *dreadfully* slow — minutes for a small image.

2. **Fast / the real way — a RAM flash loader.** Upload a tiny native routine into
   the chip's RAM **once**, then for each chunk: fill a RAM buffer with the data
   (one fast batched memory write), set the CPU's argument registers + PC to the
   loader's `program` entry, `resume`, and let the *chip's own CPU* drive the SPI
   flash at full hardware speed. You only pay JTAG cost to move the data into RAM
   and to start/wait for the loader — not per flash transaction.

**Every serious tool does #2:** esptool's "stub flasher", OpenOCD's flash drivers,
and probe-rs's flash algorithms are all RAM loaders. espjtag will too.

## Why #2 is fast (the cost model)

The bottleneck over the USB-JTAG is **round-trip count**, not raw bandwidth (USB
Full-Speed = 1 ms frames; TCK is irrelevant — see ESPJTAG-VS-OPENOCD-AUDIT.md).
The RAM-loader approach minimises round-trips:

| step | JTAG cost |
|---|---|
| upload the loader (once) | ~few ms — one batched memory write of a small blob |
| per chunk: fill RAM data buffer | **one batched memory write** (espjtag already does N words in ~1 round-trip-per-chunk via `read_mem`/`write_mem` batching) |
| per chunk: set args + PC, resume | a handful of register writes (DMI ops) |
| per chunk: wait for done | poll one register / breakpoint |

The actual flash erase + program runs on the **target CPU at hardware SPI speed**,
overlapped with nothing on the JTAG link. So throughput ≈ how fast we can stream
data into RAM (the batched memory-write path) + the chip's own program time.

## espjtag's building blocks (what we already have)

- **`write_mem32` / batched `write_mem`** — fill the RAM data buffer fast (System
  Bus Access; batching collapses N words into ~1 round-trip per chunk).
- **`read_register` / `write_register`** (abstract command) — set the loader's
  argument registers (RISC-V ABI: `a0..a7` = GPR x10..x17) and the stack pointer.
- **PC control** — set `dpc` (CSR 0x7b1) to the loader entry while halted.
- **`halt` / `resume`** — start the loader and wait for it to finish.
- **An `ebreak` return-trap pattern** — already used in the (researched) ROM-call
  recipe: point the return address (`ra`/x1) at a word containing `ebreak`
  (0x00100073) in scratch RAM, so when the loader returns it re-enters debug mode
  and we know it's done. Read `a0` for the result code.

So the flash loader is "call a function on the target via JTAG" — the same
mechanism the ROM-flash recipe (below) already specifies.

## Two loader options

### A. Call the ROM's built-in SPI-flash functions (simplest first step)
The ESP32-C6 mask ROM exports SPI-flash helpers at fixed addresses (from esp-idf
`esp32c6.rom.ld`) — no blob to upload at all:

```
esp_rom_spiflash_unlock        = 0x40000154   # once: clear block-protect
esp_rom_spiflash_erase_sector  = 0x40000144   # a0 = sector index (byte_addr / 0x1000)
esp_rom_spiflash_write         = 0x4000014c   # a0=dest byte addr, a1=src SRAM ptr, a2=len
                                              #   handles WREN / WIP-poll / 256-byte paging
```

Recipe (per the flash research): halt; stage the page data into SRAM via batched
`write_mem`; set `a0/a1/a2` (regno 0x100a/0x100b/0x100c), `ra`→an `ebreak` in
scratch, `sp`→an SRAM stack; set `dpc`→the ROM entry; resume; on the ebreak-halt
read `a0` for the result (0 = OK). Disable icache (`Cache_Disable_ICache` =
0x40000690) around it and keep the core halted. **ROM addresses are C6-specific —
needs the per-chip table (#4).** `esp_rom_spiflash_write` already chunks pages and
polls WIP, so one call can program a large region.

### B. Upload a purpose-built flash algorithm (probe-rs's approach — fastest, portable)
probe-rs (see PROBE-RS-STUDY.md) uploads a small position-independent **flash
algorithm** blob to RAM that exports `Init / EraseSector / ProgramPage / UnInit`,
calls them by setting GPRs + PC + a return breakpoint, and **compresses the page
data** (probe-rs uses miniz/zlib) before streaming it in — fewer bytes over JTAG.
probe-rs ships per-chip blobs + entry offsets in its target YAMLs (C6/C5/H2/P4). We
could reuse those exact blobs. This is the gold-standard fast path and the natural
evolution once option A works.

> Note: probe-rs flashes via its loader and so **never enters esptool's ROM
> download mode** — which is why it doesn't need the USB-reset-to-clear-strap step
> that espjtag's `reset_run_from_rom` does. A JTAG-loader flasher sidesteps the
> whole C6 post-flash-ROM-boot problem.

## SBA can't reach the XIP-flash window — use the loader, or progbuf
A gotcha probe-rs flagged: **System Bus Access cannot read the C6's
cache-mapped flash window (0x4200_0000)** — that path goes through the cache, not
the system bus. For *reading* flash to verify, route through the program buffer
(progbuf) or read the loader's verify result. For *writing*, you never write the
XIP window directly anyway — you erase+program the physical flash via the loader.

## High-speed write, end to end (the planned flow)

```
1. examine + halt the core; disable icache + the watchdogs
   (probe-rs zeroes super/TG0/TG1/RTC WDT on halt — key 0x50D83AA1 — to keep the
    core held; espjtag should too, see the audit).
2. (option B) upload the flash-algo blob to RAM once; call Init.
3. for each erase region: call EraseSector (or esp_rom_spiflash_erase_sector).
4. for each page/chunk of the image:
     a. batched write_mem the (optionally compressed) data into the RAM buffer
     b. set a0..=dest,len,src; dpc=ProgramPage; ra=ebreak-trap; resume
     c. wait for the ebreak; check the result register
5. (verify) read back via the loader / progbuf and compare.
6. UnInit; re-enable icache + watchdogs; reset-run the app.
```

## Expected speed
Bounded by the batched memory-write rate into RAM (espjtag's write path, the same
batching that made reads ~120× faster — see JTAG-BENCHMARK-ANALYSIS.md) plus the
chip's native SPI program time, **not** by per-flash-transaction JTAG round-trips.
With RLE on the OUT stream + compressed page data (#8 / probe-rs), the JTAG link
stops being the bottleneck and the chip's flash write speed dominates — i.e. on par
with esptool/OpenOCD/probe-rs flashing.

## Status checklist
- [x] memory read/write via SBA (batched) — the data-staging primitive
- [x] register + PC control + halt/resume + ebreak-trap — the "call a function" mechanism
- [x] the ROM-flash function addresses + calling convention (researched, option A)
- [ ] **option A: call esp_rom_spiflash_* to erase+program — NOT YET BUILT (#3)**
- [ ] per-chip table of ROM addrs / flash-algo blobs (#4)
- [ ] option B: upload probe-rs-style flash-algo blobs (fastest)
- [ ] watchdog-disable + icache-disable on halt (#audit)
- [ ] verify-readback + a `flash <bin>` command

Refs: espjtag #3 (flash over JTAG), #4 (per-chip data), #8 (speed),
docs/PROBE-RS-STUDY.md, docs/ESPJTAG-VS-OPENOCD-AUDIT.md,
docs/JTAG-BENCHMARK-ANALYSIS.md, esp-idf esp32c6.rom.ld.
