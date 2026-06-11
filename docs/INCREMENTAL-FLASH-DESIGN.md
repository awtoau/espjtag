# Incremental Flash over JTAG — Design & Prior-Art Survey

Consolidated because this information is genuinely hard to find — it's scattered
across esptool source, probe-rs source, and a static RE of STM32CubeProgrammer.
This doc is the single place that pulls it together for espjtag's flash engine
(#27 stub, #34 incremental).

> **Status:** design + prior-art. Substrate built this session (`call_function`,
> XDM r/w, the Tcl bridge, C6/C5 ROM-call flash as ground truth). Engine not yet
> built.

---

## 1. Goal

Flash over JTAG, in a live debug session (no download-mode reboot), **incrementally
and verified**: write only the sectors that changed, and prove they landed — fast
enough to be the inner loop of edit→build→flash.

The differentiator vs esptool/OpenOCD isn't speed alone — it's *in-session* +
*verified* + *no app cooperation needed*.

---

## 2. The core idea — on-chip Merkle hash, digests-only-on-wire

Move the **hashing to the chip**; keep only **digests** on the wire. One primitive:

```
hash_tree(region)  →  root  (+ leaves on request)
   leaves = one digest per 4 KiB sector   (the diff granularity)
   root   = digest of all the leaf digests (the "hash of hashes")
```

Three jobs collapse into reads of that tree:
- **identical-check** — compare the **root** (one digest). Match → whole region is
  already flashed → skip everything, instant.
- **diff** — root differs → pull the **leaf list** (a few KB), compare to the new
  image's leaves, write only the differing sectors. *The chip hands you the leaves —
  no "old binary" needed* (unlike esptool `--diff-with`).
- **verify** — re-hash the written sectors (same leaf routine) → confirm they match
  the expected digest. **Mandatory, and now free** (a digest exchange, not a
  read-back of the whole image).

Bonus correctness: diff-hash and verify-hash are *literally the same on-chip code*,
so they can't disagree.

For ESP-sized flash, 2 levels (root + leaves) is the sweet spot; a deeper tree only
matters for huge flash (descend only into differing subtrees, `log n` digests).

---

## 3. Prior art (the hard-to-find survey)

Every real-world tool surveyed (esptool, probe-rs, ST, ARM/pyOCD, SEGGER/Nordic)
converges on the **same loader interface** — a small routine in target RAM, exporting
fixed functions, called over the debug link with register args. Only the licence, the
transport, and the **digest** differ — and on the digest the field splits cleanly:
**ST alone uses a weak additive byte-sum (§3c); everyone else who digests at all uses a
real CRC-32 computed on-target (§3d).**

### 3a. esptool (Espressif)
- **Tool licence: GPL-2.0+** (don't copy esptool code). **Stub licence: split** —
  `stub_flasher/1/` (old C stub) is **GPL-2.0**; `stub_flasher/2/` (new
  `esp-flasher-stub`) ships **`LICENSE-APACHE` + `LICENSE-MIT`** → reusable.
- Transport: **SLIP-over-UART** (`0xC0` delim, `0xDB` escapes; `0xEF` checksum seed).
  Frame: `dir|cmd|size|checksum|payload` ↔ `dir|cmd|size|value|payload|status`.
- Commands: `SYNC 0x08`; `MEM_BEGIN/DATA/END 0x05/07/06` (upload+run stub);
  `FLASH_BEGIN/DATA/END 0x02/03/04`; `FLASH_DEFL_* 0x10/11/12` (deflate-compressed);
  **`SPI_FLASH_MD5 0x13`** (on-chip MD5 of a flash region — the hash primitive);
  `SPI_ATTACH 0x0D`; `READ/WRITE_REG 0x0A/09`.
- Incremental (current, in `cmds.py`): **`skip_flashed`** (MD5 a region, compare to
  the new image's MD5, skip if equal) + **`--diff-with <old.bin>`** (host computes
  *contiguous changed ranges* vs an old binary you supply) + `no_diff_verify`.
  → **flat region-MD5 + host range-diff against a binary you provide.** Our Merkle
  tree is strictly better (chip provides the leaves; identical-early-out; no old
  binary).
- Caveat for JTAG: it's a **byte-stream over UART** — driving it over JTAG means
  *emulating the UART* (poke RX FIFO / drain TX via JTAG memory). Ugly → favour the
  CMSIS-style algo below.

### 3b. probe-rs
- **Licence: MIT OR Apache-2.0 → reuse the actual code.**
- Flash-algorithm format (`probe-rs-target/src/flash_algorithm.rs`): CMSIS-style
  function offsets — `pc_init`, `pc_program_page`, `pc_erase_sector`, `pc_erase_all?`,
  **`pc_verify?`**, `pc_blank_check?`, `pc_read?`. Self-contained blob (its own flash
  driver → no ROM-state dependency → covers the S3 where the ROM path is a dead end).
- **JTAG-native**: register args + run-to-breakpoint — exactly what espjtag's
  `call_function` already drives. **This is the engine we reuse.**

### 3c. STM32CubeProgrammer / ST-LINK  (reverse-engineered — see `~/git/gihdra`)
RE'd statically: `~/git/gihdra/analysis/STM32CubeProgrammer_Analysis.md`,
loaders in `~/git/gihdra/tmp/flashloader_bins`, disasm via `scripts/disasm_stldr.sh`.
- **Licence: `.stldr` loaders are SLA0048 — restricted, no source, STM32-hardware
  only.** → **model the interface, never ship the binary.**
- `.stldr` = 32-bit ARM Thumb ELF, loaded to SRAM `0x20000000`, exporting:
  `Init · Write · SectorErase · MassErase · **Verify (CRC-based)** · CheckSum · WriteOB`.
  Flow: upload → `Init` (unlock) → `SectorErase` → [chunk→SRAM→`Write`]× → `Verify`.
  SRAM is the staging double-buffer. **Same CMSIS shape as probe-rs.**
- Incremental: CLI keyword **`incremental`**, path
  `DebugInterface::programBufferFlashLoader`. **Has a confirmed use-after-free** (the
  `Target::loaders` BSS table raced by the USB hotplug callback — `gihdra` GH #30;
  SIGSEGV in `ST_LINKInterface::programMemory`). Workaround: force full flash mode
  (skips the per-sector compare). *So even ST's mature impl is buggy here — note what
  not to copy: no shared mutable loader table without a lock.*

> ### ✅ RESOLVED — how ST's `incremental` ACTUALLY diffs (Ghidra decompile, `~/git/gihdra`)
> Definitively traced through clean decompilation (`ProgramManager::programMemory` @
> CLI 0x5fae50 → `DebugInterface::compareCheckSum` @ 0x20bfd0 → `DebugInterface::
> checkSum` @ 0x203410 → `Utility::getCheckSum` @ 0x2738c0):
> 1. **Per-sector, localizing.** A per-sector "modified" flag array; matching sectors
>    are SKIPPED, changed ones erase+written. One changed sector in a 1 MB image →
>    rewrite just that sector. (NOT whole-image, NOT byte read-back-memcmp — that
>    `compareMemoryWithFile` path is the `-cmp` verb, **never called** by `incremental`.)
> 2. **The digest is a 32-bit ADDITIVE BYTE-SUM, not a CRC.** `Utility::getCheckSum`
>    is literally `sum += byte`; the on-target `.stldr CheckSum` is the same additive
>    sum (two loaders disassembled — zero CRC ops). The real `CRC32` @ 0x40a340 exists
>    but is **not** on the incremental path.
> 3. **Computed host-side** (readMemory over SWD → sum in the PC) for direct internal
>    flash, or **on-target** (the `.stldr` sums on the MCU) for loader-served memories.
>    **NEVER on the probe** — recomputed every run, nothing cached (probe-CRC theory
>    refuted).
> **Implication: ST's `incremental` is genuinely UNSAFE** — an additive sum collides on
> `+1/−1` cancellation and byte reordering (exactly what compilers emit), so a changed
> sector can be silently skipped. This is the **anti-pattern**, not the recipe. We use
> a real per-sector CRC (floor) / wide digest (fleet) + verify-after-write. ST proves
> the *per-sector skip architecture*; it proves what NOT to use for the digest.
>
> **✅ CONFIRMED ON SILICON** (black-box, cites no decompilation —
> `scripts/st_incremental_proof.py`, run on a real STM32F427): flashed image A, then
> `incremental`-flashed a B whose sector 1 swapped two bytes (`0x11`↔`0x22`, **additive
> sum unchanged**) and whose sector 2 flipped one byte (sum changed). After the
> incremental download, sector 1 **still read back as A** (the `0x22` change silently
> dropped) while sector 2 **was written** — a genuine content change skipped purely
> because it preserved the byte-sum, a sum-changing one caught. Three independent
> confirmations now: host `Utility::getCheckSum` disasm, internal+external `.stldr`
> disasm (0 CRC ops), and this live differential flash.

### 3d. ARM / Keil / pyOCD  &  Nordic / SEGGER  (surveyed — vs ST's weak sum)

> **Headline:** unlike ST's additive byte-sum (§3c), **everyone else who does a
> digest does it RIGHT — a real CRC-32 (reversed poly `0xEDB88320`)**: pyOCD
> (on-target CRC32 blob), SEGGER/J-Flash (on-target SFL CRC), Nordic `VERIFY_HASH`.
> The CMSIS **algorithm layer itself** carries no content-diff — `Verify` there is a
> byte-compare and the *host* decides what to skip. So the recipe is: borrow the
> per-sector-skip **architecture** from ST, but take the **digest** from pyOCD/SEGGER
> (real CRC32), never from ST.

#### (A) ARM CMSIS-Pack / Keil µVision / pyOCD

1. **Incremental at all?** **The FLM algorithm: no. pyOCD (the host driver): yes.**
   The CMSIS-Pack *flash algorithm* (`.FLM`) is a dumb command executor — Init / Erase
   / Program / Verify — it has *no* skip-unchanged logic; "the host controller decides
   optimization (skip unchanged sectors)" (`FlashAlgo/source/template/FlashPrg.c`).
   Keil µVision adds only a coarse early-out: "The download is processed only when the
   binary was updated since the last Flash programming step" (whole-image timestamp,
   not per-sector content) — Keil µVision User's Guide, *Flash Download Configuration*.
   **pyOCD**, which drives the same `.FLM` blobs, *does* do real per-page skip (below).
2. **Granularity** — **pyOCD: per-page/per-sector.** `smart_flash` (default **True**):
   "attempt to **not program pages whose contents are not going to change** by scanning
   target flash memory" (`pyOCD/docs/options.md`). `chip_erase` = `auto|sector|chip`
   (default `sector`). Keil's own early-out is whole-image only.
3. **Compare mechanism** — **pyOCD = REAL CRC-32, on-target** (the key result, the
   anti-ST). Two modes in `pyocd/flash/builder.py`:
   `_analyze_pages_with_crc32()` (fast) vs `_analyze_pages_with_partial_read()`
   (fallback, host read-back memcmp). The fast path runs an **embedded CRC32 routine on
   the chip**: `pyocd/flash/flash.py` ships `_ANALYZER_CODE` — a Thumb blob, comment
   *"200 bytes of executable data below + 1024 byte crc table = 1224 bytes"*, ending in
   the word **`0xedb88320`** (the reversed CRC-32 polynomial). `compute_crcs()` does
   `write_memory_block32(analyzer_address, _ANALYZER_CODE)`, writes `(addr,size)` pairs,
   `_call_function_and_wait(analyzer_address, …)`, then reads back one CRC per page. Host
   side computes the expected with `binascii.crc32` → `page.crc = crc32(bytearray(data))
   & 0xFFFFFFFF`, compares `page.crc == crc`. The user-facing switch is option
   **`fast_program`** (default False): "use **CRC checks of existing flash sector
   contents** to determine whether pages need to be programmed". CMSIS `Verify` (the FLM
   function) is, by contrast, just a **byte compare** — *"Given an adr and sz compare
   this against the content of buf"* (`FlashPrg.c`), `BlankCheck` checks erased/pattern;
   neither is a CRC and both are optional.
4. **Where it runs** — pyOCD's CRC32: **on-target** (blob in target RAM, run via the
   debug link — exactly espjtag's `call_function` shape). Its fallback compare: host
   read-back. CMSIS `Verify`/`BlankCheck`: on-target (but byte-level, host-orchestrated).
5. **Licence** — **CMSIS `FlashOS.h` / FlashPrg template: Apache-2.0** (`SPDX-License-
   Identifier: Apache-2.0`, "Copyright (c) 2010–2018 Arm Limited"). **pyOCD: Apache-2.0**
   (incl. `_ANALYZER_CODE`). → **fully reusable**; the FlashOS interface is the literal
   origin of probe-rs's `pc_*` names; pyOCD's on-target CRC32 analyzer is a drop-in
   pattern for our leaf digest (just widen the digest per §9).

> **Verdict (A): does-it-RIGHT.** pyOCD = real on-target **CRC-32** per page, Apache-2.0
> — the clean reference, the opposite of ST. (CMSIS-FLM by itself does no diff; Keil's
> own skip is a whole-image timestamp.)

#### (B) Nordic nrfjprog/nrfutil  &  SEGGER J-Link / J-Flash

1. **Incremental at all?** **SEGGER J-Flash: yes** — "The data to be programmed is
   compared with the actual data in flash, and **sectors that already match the data are
   skipped** and will not be modified" (SEGGER KB, *J-Link flash programming*). J-Link
   Commander: "the comparison in the beginning is a Checksum (CRC) comparison. In case
   everything is identical, there is no erase, program, verify stage" (whole-image
   early-out). **Nordic nrfjprog: not a content-diff** — its erase is *region-scoped*
   only: `--sectorerase` "will only erase the flash ranges **targeted by the hex file**"
   (Nordic docs) — i.e. erase the sectors the image *occupies*, not the sectors that
   *changed*. No skip-unchanged mode exists (the `EraseAction` enum is
   `ERASE_NONE|ERASE_ALL|ERASE_SECTOR|ERASE_SECTOR_AND_UICR|ERASE_CTRL_AP` — all
   region-scoped, `pynrfjprog/Parameters.py`).
2. **Granularity** — SEGGER: **per-sector** skip (above) + a whole-image CRC early-out
   in Commander. Nordic: per-region erase, no per-sector content skip.
3. **Compare mechanism** — **SEGGER = REAL CRC, on-target** (the anti-ST again): "A CRC
   over the data in flash is **calculated by the flash algo** and sent back to the PC
   software to compare it against the CRC of the data to be programmed … much faster
   than read-back" — via the SEGGER Flash Loader entry **`SEGGER_FL_CalcCRC()`**
   (vs `SEGGER_FL_Read()` for the read-back path); `BlankCheck` separately lets J-Link
   skip `SEGGER_FL_Erase()` on already-erased sectors (SEGGER KB, *J-Link flash
   programming* / *SEGGER Flash Loader*). Polynomial: **CRC-32, reversed `0xEDB88320`**
   ("CRC32-CCITT polynomial … reversed form 0xEDB88320", SEGGER forum). **Nordic** rides
   the same J-Link DLL; its *verify* exposes the choice directly —
   `VerifyAction = {VERIFY_NONE=0, VERIFY_READ=1, VERIFY_HASH=2}` (`Parameters.py`), and
   `nrfjprog --fast` "increases the speed of `--verify` by **calculating the hash of the
   on-chip flash instead of reading it**" (Nordic docs). So Nordic's *verify* is a real
   on-target hash/CRC — but it's a verify, not a skip-unchanged diff.
4. **Where it runs** — **on-target** for both: SEGGER's CRC is computed by the flash
   loader on the MCU and only the 32-bit CRC crosses the wire; Nordic's `VERIFY_HASH`
   likewise hashes on-chip. **Not in the probe** (J-Link recomputes via the loaded SFL).
5. **Licence** — **SEGGER J-Link DLL / SFL: proprietary** (free for eval/non-commercial,
   restricted redistribution); Nordic nrfjprog/`pynrfjprog` wraps the closed J-Link DLL.
   `pynrfjprog` Python wrapper is permissive but the engine is the binary J-Link DLL. →
   **model the interface (it matches CMSIS/probe-rs), never ship the binary** — same rule
   as ST's `.stldr`.

> **Verdict (B): does-it-RIGHT, but proprietary.** SEGGER = real on-target **CRC-32**
> per-sector skip; Nordic = real on-target hash *verify* (no content-diff skip, just
> region-scoped erase). Mechanism is the model to copy; the code is closed → reuse
> nothing, mirror probe-rs/pyOCD's Apache implementations instead.

**What we reuse:** code from **pyOCD** (Apache-2.0) — its on-target **CRC32 analyzer**
(`_ANALYZER_CODE` + `compute_crcs`) is the closest existing thing to our leaf-digest
routine, and confirms the recipe: *real CRC-32, computed on the chip, digests-only on the
wire* — then widen the digest (§9). The **CMSIS `FlashOS.h`** interface (Apache-2.0)
formalises the loader shape probe-rs already gives us. From **SEGGER/Nordic**: interface
confirmation only (closed binaries — model, don't ship). Net: **four independent tools now
agree the digest should be a real CRC computed on-target; ST's additive sum is the lone
outlier and the one anti-pattern.**

---

## 4. The convergence

```
            Init / Erase / Program / Verify   ← the universal loader interface
            ┌──────────────┬──────────────┬─────────────────┐
   ST .stldr (SLA0048)   probe-rs (MIT/Apache)   esp-flasher-stub v2 (Apache)
   RE-proves the iface    THE REUSABLE ENGINE      Apache, but SLIP-over-UART
   + the CRC primitive    (call_function drives)   (reference for ESP specifics)
```

espjtag's `call_function` is built to drive exactly this shape (call0 entry,
register args, run-to-trap). So the engine is **probe-rs's Apache CMSIS algorithm**;
ST and ARM *prove the interface*; esptool is the *ESP-specific reference* (and its
v2 stub is Apache if we ever want it).

---

## 5. Licence map (the hard rule)

| Source | Licence | Use |
|---|---|---|
| espjtag | Apache-2.0 | — |
| probe-rs flash algo | MIT/Apache | **REUSE the code** |
| esp-flasher-stub v2 / espflash stubs | Apache/MIT | reuse / reference |
| ARM CMSIS FlashOS | Apache-2.0 (confirmed, §3d) | reuse iface |
| pyOCD (CRC32 analyzer) | Apache-2.0 (§3d) | **REUSE the code** |
| SEGGER J-Link DLL / SFL | proprietary | model iface, never ship |
| Nordic nrfjprog / pynrfjprog | wraps closed J-Link DLL | model iface, never ship |
| esptool **tool** | GPL-2.0+ | **read for protocol only, never copy code** |
| esptool stub **v1** | GPL-2.0 | **don't use** (use v2) |
| ST `.stldr` loaders | SLA0048 restricted | **model interface, never ship binary** |

---

## 6. Our design = the delta

1. **Engine**: reuse **probe-rs's Apache CMSIS flash algorithm** per arch (RISC-V for
   C6/C5, Xtensa for S3 — its self-contained driver sidesteps the S3 torn-down-ROM
   wall, #29).
2. **Hash**: extend `Verify` into the **Merkle `hash_tree(region)→root/leaves`**.
3. **Transport**: drive via the proven **`call_function`** + a small **RAM mailbox**
   (memory-poll handshake, or — neatly — poke the SW-interrupt register so a
   resident helper services it without halting; see §8).
4. **Layout**: pair with **change-localizing linker layout** (§7) — without it the
   diff finds nothing to skip.

---

## 7. Linker layout — the multiplier (proven in `awto-l8`)

Incremental only bites if a small source change touches few sectors. `awto-l8`'s
`STM32F427VGTX_FLASH.ld` already does this by hand:
- `.text_extlibs/.rodata_extlibs → FLASH_EXT_FAKE` — stable libs pinned in their own
  region, away from volatile app code;
- custom `.FontFlashSection`/`.FontSearchFlashSection` — GUI assets `KEEP`'d in fixed
  spots; `.isr_vector` first (jump target).

**Principle:** stable content at fixed addresses, volatile code isolated and *last*,
so a rebuild churns only the tail. The "smart linker" idea: given the sector map +
change-frequency, auto-pin stable sections to sector boundaries and pad volatile ones
to absorb growth without rippling. GNU `ld` primitives (custom sections + `ALIGN` +
`KEEP` + region assignment) get most of the way; l8 proves it.

> Note: ESP's **uniform 4 KiB** sectors are *better* for incremental granularity than
> STM32F4's **16/64/128 KiB** variable sectors (a 1-byte change in a 128 KiB sector =
> erase+write 128 KiB). Layout discipline matters more on ST; ESP is well-suited.

---

## 8. Two execution models

- **Loaded stub (universal)** — load the probe-rs algo into RAM, halt-and-run via
  `call_function`. Works on any chip/app. v1.
- **Resident cooperative helper (turbo dev mode)** — link a tiny mailbox-servicing
  component into *your* app; espjtag pokes the **SW-interrupt register over JTAG** to
  wake it (no halt). Reuses the app's *live, encryption-aware* flash driver → sidesteps
  every ROM-state wall. Constraints: dev-time only (helper must be built in; first
  flash bootstraps it); can't overwrite running code → A/B OTA-slot + reboot; needs
  the app alive (fallback to stub). v2.

---

## 9. Correctness — the failure modes (non-negotiable)

**Showstoppers (incremental can't apply):** flash encryption (ciphertext ≠ plaintext
→ detect via efuse, fall back); secure boot / signed images; data partitions
(NVS/FAT — scope to code regions only).

**Silent-corruption traps:** weak digest (CRC32 collides ~1/4B — use a wide/strong
digest); host+stub must hash **bit-identically**; **mandatory verify-after-write**;
sector alignment + partial trailing sector; cache coherency (hash **raw** flash, not
a stale XIP/icache view).

**Effectiveness:** build non-determinism (a small change can shift many sectors → no
win; needs the linker discipline); **>~30 % delta → full-flash fallback**; the diff
scan itself costs an on-chip flash read (bounded; scope to the image range).

**Robustness:** atomicity (power-loss → half-updated image; order writes safely);
mailbox handshake races; stub must not clobber the app; **no shared mutable loader
table without a lock** (the ST UAF lesson).

---

## 10. Open questions / TODO
- Resolve ST's actual `incremental` diff mechanism from `programBufferFlashLoader`
  disasm (§3c box) — read-back vs CRC vs probe-side CRC.
- ~~Survey ARM CMSIS FlashOS + Nordic nrfjprog (§3d); confirm licences.~~ **Done (§3d):
  pyOCD = real on-target CRC-32 (Apache, reusable); SEGGER = real on-target CRC-32
  (proprietary); Nordic = region-scoped erase + on-target hash *verify* (closed DLL);
  CMSIS-FLM itself carries no diff (byte-compare `Verify`, host decides skip). ST's
  additive sum is the lone weak outlier.**
- Pick the digest (CRC32+len+salt vs truncated SHA-256) — collision vs on-chip cost.
- Extract probe-rs's ESP flash-algo blobs + entry offsets; confirm they're call0.

## 11. References
- esptool: `/usr/lib/python3.x/site-packages/esptool/{loader,cmds}.py`; stubs
  `targets/stub_flasher/{1,2}/`.
- probe-rs: `probe-rs-target/src/flash_algorithm.rs`; espflash
  `resources/stubs/*.toml` (incl. `esp32s3.toml`).
- ST RE: `~/git/gihdra/analysis/STM32CubeProgrammer_Analysis.md` (§4 loader iface,
  §14 disasm, §22 incremental UAF), `tmp/flashloader_bins`, `scripts/disasm_stldr.sh`.
- l8 layout: `~/git/awto-l8/STM32F427VGTX_FLASH.ld`.
- espjtag substrate: `espjtag/xtensa.py` (`call_function`), `espjtag/debug.py`
  (C6/C5 ROM-call flash, the ground truth), `scripts/ocd_tcl_bridge.py`.
