# pyOCD incremental flash on ST-Link — silicon proof, a `fast_program` data-loss bug, and the upstream handoff

**Date:** 2026-06-12. **Bench:** STM32F427 + STLINK-V3 (sn `004D00373033510135393935`),
pyocd **0.44.1** (repo `.venv`), texane `st-flash` as the independent writer/reader.
**Script:** [`scripts/pyocd_incremental_proof.py`](../scripts/pyocd_incremental_proof.py).
Follows on from [CUBEPROGRAMMER-BUGS.md](CUBEPROGRAMMER-BUGS.md) and
[INCREMENTAL-FLASH-DESIGN.md](INCREMENTAL-FLASH-DESIGN.md) §3c/§3d.

> **Status (2026-06-12):** the bug below is **verified live at upstream git HEAD**,
> has **no existing upstream issue** (searched `fast_program` + sector/page
> skip/data-loss terms), and is **not yet reported**. The upstream-facing work
> (file the issue in §6, optionally PR the §4 patch + §5 unit test) is **being
> pursued outside this repo — nothing further happens here.** Re-run the issue
> search just before filing; that check ages. After upstream lands, drop the
> "avoid `fast_program`" warning in §7.

## 1. Why this test exists

§3c proved on silicon that CubeProgrammer's `incremental` skips a changed sector when
the change preserves ST's additive byte-sum. §3d surveyed pyOCD as the anti-ST: real
per-page CRC-32, same ST-Link hardware (the probe firmware is a fixed USB
vendor-protocol server; the host tool is freely swappable — OpenOCD, CubeProgrammer,
pyOCD all speak the same probe protocol; flash loaders — `.stldr` / `.FLM` / pyOCD's
CRC analyzer blob — run on the *target*, uploaded to SRAM over the debug link).
Claim to verify: *pyOCD already is the correct incremental flash for ST-Link, in
pure Python.* So we ran the **same differential test** against pyOCD.

Test design (identical images to `st_incremental_proof.py`, seed `0x1234`, first
64 KB = four 16 KB sectors):

- **sector 1** — adjacent-byte swap `0x11`↔`0x22` (content differs, additive sum identical)
- **sector 2** — single-byte flip `0x00`→`0xFF` (sum-changing control)
- **sectors 0+3** — unchanged (must be *skipped*, proving the run is genuinely incremental)

Setup (write A) and all read-backs use open `st-flash`; pyOCD performs only the
download under test (`smart_flash=True`, `chip_erase=sector`, target override
`stm32f429xi` — F42x/F43x share DEV_ID 0x419 and the flash sector map; the region
declares `sector_size=0x4000, page_size=0x1000`).

## 2. Results (both runs on real silicon, read back by st-flash)

| pyOCD mode | sum-preserving swap | control flip | unchanged sectors | erased / programmed / skipped | verdict |
|---|---|---|---|---|---|
| **default** (`fast_program=False`, read-back compare) | **WRITTEN** ✅ | WRITTEN ✅ | intact, skipped ✅ | 32768 / 32768 / 32768 | **PASS — correct incremental** |
| `fast_program=True` (on-target CRC-32) | changed *page* written ✅ | changed *page* written ✅ | intact, skipped ✅ | 32768 / **8192** / 57344 | **FAIL — DATA LOSS** (§3) |

- **Default mode is the proof we wanted:** same probe, same chip, pure Python —
  the sum-preserving change CubeProgrammer silently drops is caught and written,
  and the unchanged sectors are skipped. The "fix ST's incremental in a few lines
  of Python" claim holds, with `fast_program` left at its default (False).
- **`fast_program=True` loses data:** it erased the two dirty 16 KB sectors but
  re-programmed only the one changed 4 KB *page* in each, leaving the other
  12 KB per sector as `0xFF` (12306/16384 and 12317/16384 bytes read back `0xFF`).
  The operation reports success — the stats line even brags about the "skipped"
  bytes. The CRC digest itself worked; the failure is orchestration, not the hash.

Trigger conditions (all required): `fast_program=True`; a flash region with
**`sector_size > page_size`** (e.g. STM32F4: 16 KB / 4 KB); a dirty sector that
also contains ≥1 CRC-matching page (i.e. a partial sector change — the
overwhelmingly common incremental case). On parts where sector == page the bug is
invisible, which is presumably how it survived.

## 3. Root cause (`pyocd/flash/builder.py`, 0.44.1 ≈ git HEAD)

The **only** code that enforces "an erased sector must have ALL its pages
re-programmed" is the tail of `_scan_pages_for_same()` (~line 843):

```python
# If we have to program any pages of a sector, then mark all pages of that sector
# as needing to be programmed, since the sector will be erased.
for sector in self.sector_list:
    if sector.are_any_pages_not_same():
        sector.mark_all_pages_not_same()
```

Page-sameness analysis happens in `_analyze_pages_with_crc32(assume_estimate_correct)`
(~line 714):

- **`fast_program=False`** — `assume_estimate_correct=False`: only *mismatching*
  pages are marked `same=False`; CRC-matching pages remain `same=None`. The `None`s
  force `_finalize_smart_flash()` to call `_scan_pages_for_same()`, which resolves
  them by full read-back **and runs the sector-marking loop**. Correct.
- **`fast_program=True`** — `assume_estimate_correct=True`: matching pages are
  marked `same=True` *definitively*. Then `_finalize_smart_flash()` (~line 439):

  ```python
  def _finalize_smart_flash(self, progress_cb, fast_verify: bool) -> None:
      """@brief Resolve any pages still unknown after smart-flash estimation."""
      if not any(page.same is None for page in self.page_list):
          return                     # <-- nothing is None, so this returns...
      self._compute_sector_erase_pages_and_weight(fast_verify)
      if any(page.same is None for page in self.page_list):
          self._scan_pages_for_same(progress_cb)   # <-- ...and the ONLY caller of
                                                   #     the marking loop never runs
  ```

`_sector_erase_program*` / `_erase_sectors` then erase every sector with
`are_any_pages_not_same()` but program only pages with `same is False` — the
`same=True` pages inside erased sectors are "skipped" straight into `0xFF`.

Caller flow for orientation: `program()` → `_prepare_sectors_and_pages()` →
erase-strategy choice (`_compute_sector_erase_pages_and_weight(fast_verify)` runs
the CRC analysis here on the fast path) → `_finalize_smart_flash()` → sector
erase + page program.

Note this is **not** the documented CRC-collision risk (`fast_program`'s docstring
admits a ~2⁻³² false-match chance). The CRCs here were all correct; the
orchestration loses data even with a perfect digest.

## 4. Suggested fix

Run the sector-marking invariant unconditionally after analysis, instead of only
inside `_scan_pages_for_same()`:

```python
def _finalize_smart_flash(self, progress_cb, fast_verify: bool) -> None:
    if any(page.same is None for page in self.page_list):
        self._compute_sector_erase_pages_and_weight(fast_verify)
        if any(page.same is None for page in self.page_list):
            self._scan_pages_for_same(progress_cb)
    # A sector containing any not-same page will be erased, so every page in
    # that sector must be programmed — regardless of which analysis ran.
    for sector in self.sector_list:
        if sector.are_any_pages_not_same():
            sector.mark_all_pages_not_same()
```

The duplicate marking (also still in `_scan_pages_for_same`) is idempotent and can
be left or removed. Two side effects to check in review: `sector_erase_weight` /
page weights are computed before marking on the fast path, so the progress
estimate slightly undercounts (cosmetic); the perf stats (`skipped_byte_count`
etc., ~line 617) are derived from `page.same` after programming, so they become
consistent automatically.

## 5. Test strategy

- **Unit (no hardware, upstream-suitable):** pyOCD's `test/unit/` has a mock-flash
  builder test harness. Add a case with a region `sector_size = 4 * page_size`,
  pre-populate "current flash" so one sector has 1 changed + 3 unchanged pages,
  run the builder with `fast_verify=True`, assert every page of the dirty sector
  is in the program set. Fails before the §4 patch, passes after.
- **Silicon:** `scripts/pyocd_incremental_proof.py` (`--no-fast` = the passing
  control). Needs any STM32F4 + any ST-Link + `st-flash`; nothing ESP-specific.
  PASS = `programmed == erased` for partially-changed sectors and full read-back
  equality. Console logs from the original runs: `tmp/pyp_run.log`,
  `tmp/pyp_run_nofast.log`, `tmp/pyp_incr.log` (untracked).

## 6. Paste-ready upstream issue draft

> **Title:** `fast_program=True` causes silent data loss on targets with sector_size > page_size (erased sectors only partially re-programmed)
>
> **Environment:** pyocd 0.44.1 (also present at current git HEAD), Linux x86-64,
> STLINK-V3, STM32F427 (target_override `stm32f429xi`).
>
> **Steps:** flash image A; modify one 4 KB page within a 16 KB sector; program
> with `-O smart_flash=true -O fast_program=true -O chip_erase=sector`.
>
> **Expected:** dirty sector erased and fully re-programmed; unchanged sectors skipped.
>
> **Actual:** dirty sector erased, only the changed page programmed; the other
> 12 KB read back `0xFF`. Log: `Erased 32768 bytes (2 sectors), programmed 8192
> bytes (2 pages), skipped 57344 bytes (14 pages)` — independently confirmed by
> read-back with st-flash.
>
> **Cause:** with `assume_estimate_correct=True`, `_analyze_pages_with_crc32`
> resolves matching pages to `same=True`, so `_finalize_smart_flash` early-returns
> and `_scan_pages_for_same` — the only place that calls
> `sector.mark_all_pages_not_same()` for dirty sectors — never runs. The
> sector-erase programmer then erases sectors with any not-same page but programs
> only the not-same pages. Affects any region where sector_size > page_size and a
> dirty sector contains a CRC-matching page. Not the documented CRC-collision
> caveat — digests were all correct.
>
> **Suggested fix:** run the mark-all-pages-in-dirty-sectors loop unconditionally
> in `_finalize_smart_flash` after analysis (patch attached/PR to follow).

## 7. Implications for this repo

1. **For the prior-art survey (§3d):** pyOCD's *digest* is right (real CRC-32) and
   its *default* incremental mode is correct on silicon, but its CRC fast path has
   a **granularity-desync** data-loss bug. Bug taxonomy across vendors:
   ST = unsound digest (additive sum); pyOCD `fast_program` = sound digest, broken
   erase-unit/write-unit coupling. Two different ways incremental flash rots.
2. **For espjtag's own incremental engine** (landed in `f166582`): add the
   invariant as a regression test — **never skip a write-unit that lies inside an
   erase-unit being erased**. On ESP32 flash the erase sector and our diff
   granularity are both 4 KB so the desync can't currently occur, but the
   invariant should be asserted in code/test so a future granularity change can't
   reintroduce pyOCD's bug class. (This does not depend on the upstream fix.)
3. **Recommendation for STM32 bench flashing:** `pyocd flash` with defaults
   (`smart_flash` on, `fast_program` **off**) — correct incremental; avoid
   CubeProgrammer `incremental` (unsound digest, plus the two-probe SIGSEGV in
   [CUBEPROGRAMMER-BUGS.md](CUBEPROGRAMMER-BUGS.md)); avoid `-O fast_program=true`
   until the upstream fix.
