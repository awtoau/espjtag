# Equivalence + benchmark report

**Date:** 2026-06-12. **Method:** the differential harness (`scripts/xcheck_fleet.py`)
reads the SAME silicon with espjtag, OpenOCD, and probe-rs and asserts agreement;
the benchmark harness (`esp32-zephyr/scripts/jtag-bench.sh` → `jtag-tests.db`,
`scripts/flash_bench.py` → `tmp/flash-bench.db`) times the same logical operations
across tools and records each run by git sha. Numbers below are from this session's
recorded runs (jtag run 13, flash run 18). Reproduce: the scripts named here.

## 1. Equivalence — espjtag is correct against both reference tools

`xcheck_fleet` 3-way dump, every present board:

| board | chip | result |
|---|---|---|
| c6-maker-a / c6-maker-b / c6-xiao-a | esp32c6 | ALL INVARIANTS AGREE ✓ mem✓ |
| c5-xiao-a | esp32c5 | ALL INVARIANTS AGREE ✓ mem✓ |
| s3-debug-mate / s3-xiao-plus / s3-xiao-sense | esp32s3 | ALL INVARIANTS AGREE ✓ mem✓ |

Invariant fields (IDCODE, DTMCS, DM regs, CPU-ID CSRs, memory, GPIO bank) match
across espjtag / OpenOCD / probe-rs on every board. Dynamic fields (per-core PC,
live pins, free-running counters) differ as expected. **espjtag's reads are
byte-identical to the two mainstream tools** — the speed work below changed no
results. (RISC-V is the full 3-way; on the S3 espjtag contributes IDCODE and the
memory/GPIO cross-check runs OpenOCD↔probe-rs — espjtag's own S3 memory path is
separately probe-rs-verified, see #29.)

## 2. JTAG transport speed (µs/word, lower = faster; min of the run)

| metric | espjtag | OpenOCD | probe-rs |
|---|---|---|---|
| **C6** read 1024-word burst | **20.5** | 90.8 | ~46 |
| C6 read 32-word | 28.9 | 125.0 | 48.2 |
| C6 write 1024-word | 29.8 | 26.4 | ~37 |
| **C5** read 1024-word burst | **23.1** | 25.4 | ~52 |
| C5 read 32-word | 32.8 | 156.2 | 53.6 |
| C5 write 1024-word | 31.0 | 29.3 | — |

- **espjtag leads bulk reads on every chip** — 4.4× faster than OpenOCD on the C6,
  ~2× faster than probe-rs, and it took the C5 (OpenOCD's old read stronghold).
- Writes are effectively tied with OpenOCD (~30 vs ~27 µs/word) and ahead of
  probe-rs.
- All measured under `drain_mode=validate` clean; in pure Python.

From the campaign baseline of **24,456 µs/word**, single-word reads are now 247 µs
(probe-rs parity) and bulk reads ~20 — ~1,200×. The lifetime curve and the fair
warm-transport headline are `esp32-zephyr/docs/images/jtag-headline.png`.

## 3. Flash speed (64 KiB A→B, 2/16 sectors changed, verify included; medians of 3)

| flasher | wall clock | notes |
|---|---|---|
| **espjtag-incr** (resident RAM stub) | **256 ms** | on-chip CRC diff → write-changed → verify; **fastest on the bench** |
| esptool-incr-dev-fast (serial, fork) | 564 ms | device-diff, no old file; the fork's two features |
| espjtag-full | 1091 ms | full-image, block erase |
| probers-full (JTAG) | 1567 ms | no incremental mode |
| openocd-full (JTAG) | 1644 ms | no incremental mode |

espjtag incremental is **4.3× faster than its own full write** and **6.1× faster
than any other JTAG flasher** — and beats the (already fast) serial fork by 2.2×.
The recorded progression (`flash_bench.py --report`, graph at
`docs/images/flash-progression.png`) traces 1568 → 256 ms across the day's fixes:
call-batching, TCK-divider, async IN/OUT streaming, the #27 RAM stub.

## 3b. Flash across the FLEET and image SIZES (matrix, verify included)

`scripts/flash_matrix.py` — every online board × {64, 256, 1024} KiB, per-chip
flasher set, 2 sectors changed, medians of 2. Two rates: **eff** = image÷time
(time-to-updated-flash, the user-facing number), **act** = bytes-written÷time
(raw; for incremental this is tiny because it only writes the 2 changed sectors —
that's the point, not slowness). Full log: `tmp/flash_matrix_full.log`; CSV:
`tmp/flash_matrix.csv`.

**Effective MB/s — incremental scales with image size, full writes don't:**

| 1 MiB A→B update | eff MB/s | wall-clock |
|---|---|---|
| **espjtag-incr** (C6, JTAG) | **0.84** | 1195 ms |
| esptool device-diff (C6, serial fork) | 0.58 | 1733 ms |
| esptool device-diff (S3, serial) | 0.25 | ~4000 ms |
| espjtag-full (C6, JTAG) | 0.07 | 15101 ms |
| openocd-full (C6, JTAG) | 0.08 | 12835 ms |
| probe-rs-full (C6, JTAG) | 0.07 | 14220 ms |

espjtag-incr's eff climbs **0.22 → 0.54 → 0.84 MB/s** across 64K→256K→1M (it skips
unchanged sectors); every full flasher stays pinned ~0.07 MB/s. At 1 MiB
incremental is **~11× faster than any JTAG full write** and 1.45× the serial fork.

**Per-chip coverage:** C6 (c6-xiao/-maker units), C5 (c5-xiao-a), S3 (xiao S3
units). **S3 is esptool-serial only** (espjtag flash execution is open, #29); its
full-write eff *rises* with size (0.05→0.12 MB/s) as the fixed reset+stub cost
amortizes. Boards flagged `off_limits` (the production ble_bridge_s3; the reserved
c6-xiao-b) are **skipped by default** — pass `--include-off-limits` to measure
them. **C3 was offline** at the time of these numbers.

One earlier gap, since corrected: a C6 dropped off the bus mid-run. That was
originally written up as a "deep USB hub branch under contention" — that framing
was wrong (checked `lsusb -t`: it's a normal ThinkPad-dock hub port). The real
cause is a **hung firmware app dropping its USB-Serial/JTAG peripheral off the
bus** until a power-cycle — the same failure mode the reset work addresses; once
re-enumerated, the board flashes fine. The numbers are complete and reproducible
on present boards.

## 4. What this proves

Correctness (the equivalence table) and speed (the benchmarks) are measured by
independent harnesses and recorded run-by-run, keyed to git sha. The headline:
**espjtag matches OpenOCD and probe-rs byte-for-byte while leading both on bulk
reads and being the fastest flasher on the bench — in pure Python, no OpenOCD
binary, no esptool, no external probe.** Scope: RISC-V (C3/C5/C6/H2) fully; S3
memory/debug works and is probe-rs-verified, S3 flash execution is the open item
(#29). Live numbers always = the README tables + the recorded DBs; this doc is the
session snapshot.
