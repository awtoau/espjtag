# pyOCD incremental flash on ST-Link — silicon proof (+ a bug found, handled elsewhere)

**Date:** 2026-06-12. **Bench:** STM32F427 + STLINK-V3 (sn `004D00373033510135393935`),
pyocd **0.44.1** (repo `.venv`), texane `st-flash` as the independent writer/reader.
**Script:** [`scripts/pyocd_incremental_proof.py`](../scripts/pyocd_incremental_proof.py).
Follows on from [CUBEPROGRAMMER-BUGS.md](CUBEPROGRAMMER-BUGS.md) and
[INCREMENTAL-FLASH-DESIGN.md](INCREMENTAL-FLASH-DESIGN.md) §3c/§3d.

## The proof

§3c proved on silicon that CubeProgrammer's `incremental` skips a changed sector
when the change preserves ST's additive byte-sum. The same differential test
(identical images, seed `0x1234`: sector 1 = sum-preserving byte swap, sector 2 =
sum-changing control flip, sectors 0+3 unchanged), run against **pyOCD** on the
same probe + chip, with all setup/read-back by open `st-flash`:

| pyOCD mode | sum-preserving swap | control flip | unchanged sectors | verdict |
|---|---|---|---|---|
| **default** (`fast_program=False`) | **WRITTEN** ✅ | WRITTEN ✅ | intact, skipped ✅ | **PASS — correct incremental** |
| `fast_program=True` | page written, **rest of erased sector lost to 0xFF** | same | intact, skipped | **FAIL — data loss** (stub below) |

**Default-mode pyOCD is the correct incremental flash for ST-Link, in pure
Python** — it catches the exact change ST silently drops and skips what didn't
change. That validates the survey's §3d recommendation: per-sector-skip
architecture + a real digest.

## The `fast_program` bug — stub only

`-O fast_program=true` silently loses data on targets where
`sector_size > page_size` (STM32F4 etc.): dirty sectors are erased but only the
changed pages re-programmed. Found here, confirmed live at upstream git HEAD,
unreported upstream as of 2026-06-12. **The full root-cause analysis, suggested
patch, unit-test recipe, and paste-ready upstream issue draft were removed from
this doc — the upstream filing is being pursued outside this repo.** If you need
that material, it's in git history: `git show bcd8a2d:docs/PYOCD-INCREMENTAL-PROOF.md`
(§3–§6).

## Implications for this repo

1. **Bug taxonomy for the survey:** ST = unsound digest (additive sum);
   pyOCD `fast_program` = sound digest, broken erase-unit/write-unit coupling.
   Two different ways incremental flash rots.
2. **espjtag's own engine is guarded against the second class:**
   [`scripts/incremental_invariant_test.py`](../scripts/incremental_invariant_test.py)
   (hardware-free) asserts *written covers every erased byte* against the real
   `flash_incremental`; the constraint is also commented at the erase loop in
   `debug.py`.
3. **STM32 bench flashing recommendation:** `pyocd flash` with defaults
   (`smart_flash` on, `fast_program` **off**). Avoid CubeProgrammer `incremental`
   ([CUBEPROGRAMMER-BUGS.md](CUBEPROGRAMMER-BUGS.md)) and avoid
   `-O fast_program=true` until the upstream fix lands.
