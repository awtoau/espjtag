# Fleet flash benchmark — process & status

How the cross-fleet flash benchmark works, how to run it, and what guarantees it
gives. The blocker tracker (#39) and the C5 probe issue (#38) that gated the first
clean run are both **closed** — their fixes are described under *Status* below.

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
4. **Stable-serial device resolution.** Boards are pinned by their USB serial (the
   MAC, colon/case tolerant), not the volatile bus-port path — so a board that
   re-enumerates mid-run is still found at its new path instead of dropping out.
5. **off_limits respected.** Boards flagged `off_limits` in `esp32-devices.json`
   (production firmware, or reserved units) are **skipped by default**; pass
   `--include-off-limits` to flash them deliberately.

## How to run

```
.venv/bin/python scripts/flash_matrix.py --sizes 64,256,1024 --rounds 2
```

Options: `--sizes` (KiB CSV), `--rounds`, `--changed` (sectors), `--addr`
(default `0x300000`, a raw test offset — NOT an app image).

A healthy run ends `ALL CLEAN`. A `!!! FAIL-FAST` line names the exact
board/size/flasher that tripped and points at the full log.

## Status

The harness is complete and the blockers that gated the first clean run are fixed
(all committed). What changed, and why:

- **C5 `openocd-full` flash-probe** (#38): after a JTAG/SoC reset the C5's MSPI
  (flash) clock is left unset, so `program_esp`'s probe sees `0 KB`. Fix: an
  esptool serial "MSPI-clock heal" (`--after no-reset`) before openocd re-inits the
  clock the bootloader's documented way; an espjtag JTAG reset restores a clean
  running state after. (`esp appimage_offset -1` was tested and does *not* help —
  the benign boot-time appimage notes are ALLOW-listed with that proof.)
- **Stable-serial resolution** (was a code bug, not USB): `EspUsbJtag(serial=…)` +
  per-round re-resolution of the path from the serial, so a re-enumerated board is
  found, not dropped (`no 303a:1001 matching '…'`).
- **openocd `:3333` port race**: a one-shot `program_esp` never needs the gdb/tcl/
  telnet servers, so they're disabled — kills the "Address already in use" abort.
- **esptool `--after hard-reset` USB re-enumeration race** (openocd-esp32 #316/#342):
  the RTS hard-reset drops the USB-Serial/JTAG peripheral off the bus and
  re-enumerates it, racing the next tool's descriptor read (`libusb -9`). Replaced
  the inter-tool resets with espjtag's JTAG `ndmreset` (no re-enumeration) — ~7×
  faster and the race is gone. See `docs/RESET-WITHOUT-REENUMERATION.md`.
- **Independent-verify retry split** (#43): a serial-port *contention* error
  ("could not open" / "resource busy" — another process owns the port) is now
  reported as contention, not retry-raced; only genuine chip-mid-reboot errors are
  retried. A byte mismatch is never retried.
- **Temp images** now go to workspace `./tmp/`, not system `/tmp`.

**Known bench-only quirk (not user-facing, won't-fix):** alternating `openocd-full`
and `espjtag` flashing the *same* chip back-to-back can intermittently trip
`flash erase block -> 1` on a C6 (espjtag flash is 3/3 clean in isolation and after
a clean openocd; only the mixed interleave the bench does trips it). Users never
interleave the two flashers on one board, so there's no shipped-path impact (#41,
closed).

## Related issues

- **#38** — C5 OpenOCD flash-probe (fixed; MSPI-clock heal).
- **#39** — P0 blocker tracker (closed; all blockers fixed).
- **#41** — bench-only C6 openocd↔espjtag interleave (closed, won't-fix).
- **#43** — verify contention-vs-transient split (fixed).
- **#29** — S3 flash-over-JTAG (why S3 is esptool-serial-only here).
- **#13** — cross-platform USB reset/recovery.
