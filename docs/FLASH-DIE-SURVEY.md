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
- Open idea: espjtag could read RDID/SFDP itself over JTAG via SPI1 register
  access (no stub, no serial) — would make the survey a pure-espjtag one-liner
  and let `flash_incremental` log the die it's writing to.
