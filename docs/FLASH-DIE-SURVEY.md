# Flash die survey — who made each board's flash, how much timing varies, and how chips discover it

**Date:** 2026-06-12. **Tool:** [`scripts/flash_chip_survey.py`](../scripts/flash_chip_survey.py)
(JEDEC ID + SFDP via esptool, timings measured over JTAG via espjtag ROM calls,
medians of 4 sectors at the 0x300000 test window). Raw run: `tmp/flash_chip_survey.log`.

## The fleet's dies and measured per-sector timings

| board | flash die | size | SFDP | erase 4K | program 4K | ROM-read 4K |
|---|---|---|---|---|---|---|
| c5-xiao-a | Puya `85:2017` | 8 MB | yes | 45.1 ms | 22.3 ms | 12.1 ms |
| c6-maker-a | Winbond `ef:4017` | 8 MB | yes | 61.3 ms | 17.2 ms | 8.7 ms |
| c6-maker-b | Winbond `ef:4017` | 8 MB | yes | 62.3 ms | 18.1 ms | 9.3 ms |
| c6-xiao-a | XMC `20:4016` | 4 MB | yes | 47.2 ms | 19.2 ms | 9.1 ms |
| c6-xiao-b | unidentified `46:4016` | 4 MB | yes | **24.1 ms** | **12.9 ms** | 9.3 ms |

Four different manufacturers across five boards — including two *different* dies
on the two "identical" Seeed XIAO C6s.

### Who is vendor `0x46`? (investigated — deliberately unnamed)

Chased through JEDEC, software databases and community logs (2026-06-12):
the official JEP106 bank-1 holder of `0x46` is **Silicon Spice** — a defunct
1990s telecom company that never made NOR flash — and no practical database
names a bare-`0x46` SPI NOR vendor (esp-idf's per-vendor drivers: generic
handles it; esptool, flashrom-world lists, Tasmota/ESPHome device logs: no
hit; flashrom's PMC PM25LQ032 uses continuation-coded `7F 9D 46`, not bare
`0x46`). Conclusion: an **unregistered/ID-squatting low-cost fab** — common in
the Chinese NOR market. We therefore report it by evidence, not name:

- RDID `46 40 16` (repeats cyclically on over-read — standard behaviour)
- SFDP rev 1.6, 3 parameter tables; basic table fingerprint
  `e520f9ffffffff0144eb086b083b42bbfeffffffffff00ffffff40eb0c200f5210d800ff`
  (standard 4K/`20` 32K/`52` 64K/`D8` erase trio, QE in SR2, 32 Mbit);
  vendor table at `0xD0` re-states ID `0x46`, no JEP106 bank/continuation
- Behaviourally excellent: fastest eraser on the bench (24 ms/sector,
  88 ms/block), all flash ops verified correct across every soak

`espjtag --info` labels it `unregistered-0x46`. If a name ever surfaces, the
SFDP fingerprint above is the match key.

**"Is the fast die lying?" — tested, no.** 300 erase+program cycles with full
verify every cycle (`tmp/die46_endurance_spot.log`): 0 failures; erase drifted
*up* +1.6 ms (genuine oxide-stress physics — a shallow-erase cheat trends fast
and constant, then fails verify); erase floor of 8.4 ms on mostly-erased
content reveals **adaptive verify-based erase** — the die pulses until cells
verify, the opposite of faking completion. Like-for-like it's 2× faster than
the same-capacity XMC die; our "slow" Winbond is an older-gen 64 Mbit part.
Unverifiable on a bench and therefore the honest residual risk: long-term
retention and full endurance (300 cycles ≈ 0.3% of spec) — exactly where cheap
dies cut corners invisibly. Fine for test boards; qualify the vendor for a
product BOM.

## How much the timing varies (and what it explains)

- **Erase: 2.6× spread** (24–62 ms/sector) — the dominant variable.
- **Program: 1.7× spread** (13–22 ms / 4 KiB).
- **Read: flat** (~9 ms, C5 12 ms) — reads are transport/CPU bound, the die barely matters.

This fully accounts for the cross-board benchmark spread: a 16-sector full flash
≈ 16 × (erase + program) + fixed overhead → predicts c6-xiao-b at
16×(24+13) ≈ 0.6 s of die time vs Winbond's 16×(62+18) ≈ 1.3 s — exactly the
1975 ms vs ~2620 ms measured in the fleet bench. The same two boards differ by
**~2%** on pure JTAG transport metrics. **Rule for reading our benchmarks:
same-board trends are the valid progression signal; cross-board flash deltas
are largely the die lottery.** The two same-die boards (c6-maker-a/-b) agree
within ~2% on everything — the harness itself is tight.

## How a chip knows how to set up different memories

Four mechanisms, layered:

1. **JEDEC RDID (cmd `0x9F`)** — 3 bytes: manufacturer, device family, capacity
   (`ef:4017` = Winbond, 64 Mbit). This is what `esptool flash-id` shows and how
   "detect flash size" works. It's an index into *driver knowledge*, not a
   self-description.
2. **SFDP (cmd `0x5A`, JESD216)** — the actual self-description: standardized
   parameter tables in the die (erase opcodes + sizes, typical/max erase and
   program times, fast-read modes, QE bit location). Every die on our bench has
   it (`esptool read-flash-sfdp`). Tools *could* derive timings/geometry from
   SFDP alone; most don't fully.
3. **ESP-IDF's actual practice** — the bootloader takes mode/freq/size from the
   **image header** (the bytes esptool patches at flash time), and the IDF flash
   driver selects a **per-vendor chip driver** (`chip_generic`, `chip_winbond`,
   `chip_xmc`, `chip_gd`, ...) by RDID at runtime for quirks: QE-bit location,
   **suspend/resume support**, unlock sequences. So vendor differences are
   handled by lookup tables in software, with SFDP as a fallback for unknowns.
4. **The mask-ROM legacy layer espjtag uses** — `g_rom_spiflash_chip` geometry
   set by `esp_rom_spiflash_config_param` (we use the universal 4 KiB sector /
   256 B page defaults, which every NOR die honours; only *timings* differ, and
   the ROM functions poll busy rather than assume them — which is why espjtag
   works unmodified across all four vendors).

Tie-back to #33: per-vendor **suspend** behaviour is the suspected mechanism
behind the SPI1 wedge when halting mid-flash-IO (the C5 board carries a Puya
die; IDF enables suspend where the die supports it). The recovery ladder fixed
the symptom; the vendor dimension is why it reproduces more readily on some
boards than others.

## Reporting

- Die identity is now recorded per board in the fleet DB
  (`esp32-zephyr/scripts/esp32-devices.json`, `flash` field) and shows up in
  this survey's one-command table.
- Cross-board flash comparisons in benchmark write-ups should cite the die
  (e.g. "c6-xiao-b (fast `0x46` die)").
- ~~Open idea: espjtag could read RDID itself over JTAG~~ **Done**: `flash_info()`
  executes JEDEC RDID directly on SPI1 via register access over SBA (no stub, no
  ROM call, no serial) — `python -m espjtag <usb> --info`. Verified against
  esptool on three different dies incl. the two-TAP C5.

## "Can we ignore the vendor and just make flash faster?" — yes, but not with clocks

Erase and program time are **die-internal physics** (charge pumps); SPI clock
tweaks (80 MHz, QIO — what esptool/IDF already expose) only speed *transfer and
reads*, which our table shows are not the bottleneck. Overdriving the clock
beyond datasheet (flashrom-community style) buys nothing on the write path.

The vendor-independent levers that DO work, in measured order:

1. **Bigger erase units** — measured on all three vendors: one 64 KiB block
   erase vs 16 sector erases = **4.4–5.7× faster** (Winbond 987→211 ms,
   Puya 741→130 ms, the 0x46 die 391→88 ms). Every NOR die honours
   sector/block/chip erase per JEDEC; esptool's stub already exploits this —
   and now espjtag's `flash_write` does too (block erase for fully-covered
   aligned 64 KiB spans only — the partially-covered ends stay sector-erased,
   per the never-erase-what-you-won't-rewrite invariant). espjtag-full 64 KiB:
   2453 → 1918 ms on the gate board.
2. **Don't write at all** — the incremental engine (#34): unchanged sectors cost
   zero erase AND zero program, the only optimization that beats the die.
3. **Skip erase when bits only fall** — `erase="auto"` in-place overwrites
   (NOR can clear bits without erasing): wins on append/bit-clear patterns plus
   saves a wear cycle.
4. **Suspend-aware scheduling** (unexplored) — dies with suspend support (a
   per-vendor RDID-keyed property in IDF's chip drivers) can suspend an erase to
   service reads; relevant to #33's wedge, not yet a speed lever for us.

Found along the way: `flash_write(verify=True)` overflowed the scratch window
for images > ~4 KiB (single whole-image `flash_read_rom` — clobbered the trap
word and wedged the session). First exercised at 64 KiB during the block-erase
bench; verify now reads back in scratch-sized chunks.
