# Fleet flash benchmark — process & status

How the cross-fleet flash benchmark works, how to run it, what guarantees it
gives, and what's currently blocking a clean full run. Kept in sync with the
tracker issue: **[#39 — P0: fix blockers before the next full fleet run](https://github.com/awtoau/espjtag/issues/39)**
(and the C5-specific **[#38](https://github.com/awtoau/espjtag/issues/38)**).

## What it measures

Flash speed across the **whole ESP fleet** (C6 / C5 / C3 RISC-V + S3 Xtensa) ×
image **sizes** {64, 256, 1024} KiB × a per-chip set of **flashers**, each cell
the median wall-clock of N A→B updates (2 sectors changed by default), **verify
included**. Two throughput columns per cell:

- **eff** (effective) = `image_size / time` — time-to-updated-flash, the
  user-facing number.
- **act** (actual) = `bytes_actually_written / time` — raw throughput. For
  incremental flashers this is tiny because only the changed sectors are written
  (that's the point, not slowness).

Flasher sets:
- **RISC-V (C6/C5/C3):** `espjtag-full`, `espjtag-incr` (pure-Python JTAG),
  `esptool-incr-dev-fast` (serial fork), `openocd-full`, `probers-full`.
- **Xtensa (S3):** `esptool-full`, `esptool-incr-dev-fast` (serial only — espjtag
  S3 flash execution is open, **[#29](https://github.com/awtoau/espjtag/issues/29)**).

## Trust model — why the numbers are believable

1. **Independent cross-tool verify.** espjtag flashes over **JTAG**, then
   **esptool reads the region back over SERIAL** and byte-compares
   (`verify_independent()` in `scripts/flash_bench.py`). Different transport,
   different codebase — no self-certification. (probe-rs XIP read returns
   unmapped `0addbad0`; OpenOCD flash-read is itself C5-flaky — esptool serial is
   the reliable independent path.)
2. **Fail-fast generic audit.** After every single flasher invocation, that
   invocation's log slice is scanned by `scripts/audit_bench_log.py` for ANY
   warning/error indicator. The first hit **aborts the whole run** — no
   retry-masking, no grinding to the end and reporting later. The auditor is
   **generic and forward-looking** (a new, never-seen warning trips it next run),
   not a hardcoded catalogue. Reviewed third-party cosmetic notes go in a short
   `ALLOW` list, each with a written reason.
3. **Single full log.** Every tool's full stdout/stderr for every invocation goes
   to one file: `tmp/flash_matrix_full.log` (plus a CSV at `tmp/flash_matrix.csv`).

## How to run

```
.venv/bin/python scripts/flash_matrix.py --sizes 64,256,1024 --rounds 2
```

Options: `--sizes` (KiB CSV), `--rounds`, `--changed` (sectors), `--addr`
(default `0x300000`, a raw test offset — NOT an app image).

A healthy run ends `ALL CLEAN`. A `!!! FAIL-FAST` line names the exact
board/size/flasher that tripped and points at the full log.

## Current status (HEAD `b155e85`, 2026-06-14)

**Working & committed:** the harness, fail-fast audit, independent verify, clean
tool output (esptool deprecations removed + `--no-compress`; OpenOCD kept on
`program_esp`, its un-silenceable boot-time appimage notes ALLOW-listed *with
proof* — `esp appimage_offset -1` was tested and does not help).

**Last run aborted** (correctly) at **c5-xiao-a, 256 KiB, `openocd-full`**:

```
Warn : Failed to read flash size!
Error: Failed to probe flash, size 0 KB
Error: auto_probe failed
** Clock configuration set failed **
```

The C5 first `openocd-full` round (64 KiB) passed; it fails on a *later*
invocation after prior resets piled up — the on-chip flash stub can't read SPI
flash size after a rapid JTAG CPU reset. "Clock configuration set failed" is the
downstream symptom (no bank to clock-boost), not the cause.

**Last clean numbers before the abort** (medians of 2, verify incl.):
- ble_bridge_s3 (S3, esptool serial): 64K 1121 / 256K 2570 / 1024K 8430 ms
- c5-xiao-a (C5) 64 KiB: espjtag-incr **310 ms**, espjtag-full 1027, esptool-incr
  591, openocd-full 2051, probers-full 1587 ms

## Blockers before the next full run (→ #39)

1. **C5 `openocd-full` repeat-invocation probe failure** (→ #38). Try
   `no_clock_boost`; if the 0 KB probe still aborts, settle the chip before
   `program_esp` (or run openocd first per board), root-cause the stub, or drop
   openocd-full for the C5 as a documented gap.
2. **Device resolution by stable serial, not volatile `usb_path`.** Everything in
   `flash_bench.py` keys on the bus-port string (`connect`, `run_flasher`,
   `setup_a`, `verify_after_external`). When a board re-enumerates under hub load
   the path changes and it can't be found ("esp_usb_jtag: no 303a:1001 matching
   '1-1.3.1.3.3.4'"). Resolve by serial, re-derive `usb_path` on each reconnect.
   **This is a code bug, not USB.**
3. **Temp images leak to system `/tmp`.** `flash_matrix.py:76`,
   `flash_bench.py:211,308` use `tempfile.NamedTemporaryFile` with no `dir=` —
   lands in `/tmp`. Repo rule is workspace `./tmp/`. Point them there.

## Related issues

- **#38** — C5 OpenOCD flash-probe failure (the specific abort above).
- **#39** — P0 tracker: the three blockers; gate the next full run on them.
- **#29** — S3 flash-over-JTAG (why S3 is esptool-serial-only here).
- **#13** — cross-platform USB reset/recovery (overlaps blocker #2).
