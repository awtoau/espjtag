# Acknowledgements & provenance

`espjtag` is a clean-room-ish Python reimplementation of a small slice of two
well-established open projects. It does not copy their source, but it **does**
transcribe numeric facts from them — register addresses, bit-field offsets, and
the USB-JTAG command encoding. This file credits those projects, records exactly
what was ported and from where, and surfaces the licensing question so a
maintainer can decide how to handle it. **Not affiliated with or endorsed by
Espressif, OpenOCD, or RISC-V International.**

The machine-readable pin of these sources lives in
[`upstream.lock`](upstream.lock); [`scripts/check_upstream.py`](scripts/check_upstream.py)
diffs the depended-on symbols against upstream HEAD (see *Re-merge / drift check*
below).

## What we ported, and from where

Pinned upstream baseline (both files vendored in the same repo):
**`espressif/openocd-esp32` @ `f10eceff22fb8dcd3db69bf3ebc5c70602454af6`** (`master`, 2026-06-10).

| espjtag file | Ported facts | Upstream source | Upstream license |
|---|---|---|---|
| `espjtag/constants.py`, `espjtag/transport.py` | USB-JTAG vendor protocol: the 4-bit `CMD_CLK` nibble layout (`bit0=TDI, bit1=TMS, bit2=capture`), `CMD_FLUSH=0xA`, `VEND_JTAG_SETDIV=0`, caps-descriptor `wValue=0x2000` | `src/jtag/drivers/esp_usb_jtag.c` | **GPL-2.0-or-later** |
| `espjtag/transport.py` | The TAP scan model — shift N−1 bits with TMS=0, last bit with TMS=1 → Exit1, all captured (an algorithm, mirrored, not copied) | `src/jtag/drivers/bitq.c` | **GPL-2.0-or-later** |
| `espjtag/constants.py`, `transport.py`, `debug.py` | RISC-V Debug Module register addresses (DMCONTROL=0x10, DMSTATUS=0x11, ABSTRACTCS=0x16, COMMAND=0x17, DATA0=0x04, SBCS=0x38, …) and field bit-offsets (dmcontrol haltreq=31/resumereq=30/ndmreset=1/…, abstractcs busy=12/cmderr=8, AC access-register aarsize=20/transfer=17/write=16, SBCS access=17/autoinc=16/…, DTM dmi op=0/data=2/address=34) | `src/target/riscv/debug_defines.h` | **BSD-2-Clause OR CC-BY-4.0** (SPDX dual) |

`debug_defines.h` carries `SPDX-License-Identifier: BSD-2-Clause OR CC-BY-4.0`
and is itself auto-generated from the **RISC-V External Debug Support** spec
(`riscv/riscv-debug-spec`, generator commit `22a7576`). The numbers in our
`constants.py` are the canonical RISC-V debug-spec register map.

Each ported source file in `espjtag/` carries a header naming its upstream origin.

## License-compatibility note (read this — it is the open question)

> **This is not legal advice.** It lays out the issue and cites the usual
> interpretations so a maintainer / counsel can make the call. espjtag is
> currently **Apache-2.0**; do not assume the analysis below is dispositive.

There are two distinct cases, and they are *not* equally fraught.

### 1. The RISC-V debug register/field constants — low concern

`debug_defines.h` is dual-licensed **BSD-2-Clause OR CC-BY-4.0**. The
**BSD-2-Clause** arm is permissive and one-way compatible with Apache-2.0: you
may use BSD-licensed material in an Apache-2.0 project, the only obligation being
to preserve the copyright notice and disclaimer (satisfied by this file +
per-file headers). So the *bulk* of `constants.py` — the RISC-V DM register map —
has a clean, explicitly-granted path into an Apache-2.0 project regardless of any
copyrightability debate. No GPL question arises here.

### 2. The four USB-JTAG protocol values from GPL `esp_usb_jtag.c` — the real question

These four numeric facts (`CMD_CLK` nibble layout, `CMD_FLUSH=0xA`,
`VEND_JTAG_SETDIV=0`, caps `wValue=0x2000`) come from a **GPL-2.0-or-later** file.
GPL-2.0 and Apache-2.0 are famously **one-way incompatible**: you can't relicense
GPL-2.0-covered *expression* under Apache-2.0. The question is therefore whether
these bare numbers are protected *expression* at all.

The prevailing view — which the maintainer should weigh, not take as settled —
is that **individual facts and the numbers dictated by a hardware interface are
not copyrightable**, only their creative *expression / arrangement* is:

- **Facts aren't copyrightable** (only original selection/arrangement is):
  *Feist Publications, Inc. v. Rural Telephone Service Co.*, 499 U.S. 340 (1991).
  A register address or a protocol opcode is a fact about how the silicon
  behaves, not authorship.
  <https://supreme.justia.com/cases/federal/us/499/340/>
- **Merger doctrine** — when an idea can be expressed in essentially only one way,
  expression "merges" with the idea and is unprotectable. The bits a USB-JTAG
  device requires can only be written one way to work. This was central to
  *Google LLC v. Oracle America, Inc.*, 593 U.S. 1 (2021); the Court resolved the
  case on fair use and pointedly *declined* to rule on API copyrightability, so
  the question is not authoritatively closed.
  <https://www.law.cornell.edu/supct/cert/18-956> ·
  <https://www.congress.gov/crs-product/LSB10597>
- **Practitioner/community guidance** likewise treats config-like / generated /
  fact-only content as commonly non-copyrightable (e.g. SPDX/REUSE note that
  "files … which contain no creative expression … may not be copyrightable").
  <https://reuse.software/faq/>

**What we actually did** reduces the exposure regardless of how that question
lands: we transcribed ~4 scalar values and re-expressed the *algorithm* of
`bitq.c` in our own Python — we did **not** copy code, comments, structure, or
the macro text. Re-expressing a method in clean code is the ordinary,
well-trodden way to interoperate across a license boundary.

### Recommendation (for the maintainer to accept or override)

1. **Keep Apache-2.0**, and treat the ported numbers as uncopyrightable interface
   facts — the mainstream reading. This file + the per-file headers provide the
   attribution that BSD-2-Clause requires and that good practice expects for the
   GPL-derived facts even if attribution is not strictly compelled.
2. **If a stricter posture is wanted**, the cleanest options are, in order:
   (a) add a short notice that the *four* USB-JTAG protocol constants are facts
   derived from GPL-2.0 `esp_usb_jtag.c` and used as uncopyrightable interface
   data (essentially what this section already does, made load-bearing in
   `NOTICE`); or (b) dual-note just `transport.py`'s protocol constants; or
   (c) relicense espjtag GPL-2.0-or-later to remove the question entirely (costs
   the permissive licensing — probably not worth it for four integers).
3. **Do not** copy any *text* (code, macros, comments) from the GPL files into
   espjtag. Porting must stay at the level of facts + independently-written code.

If in doubt about a specific downstream use (e.g. linking espjtag into a larger
proprietary product), get an actual legal opinion — the above is orientation, not
a ruling.

## Re-merge / drift check

Because these are hand-transcribed, upstream can change a value and we'd drift
silently. To prevent that:

- **[`upstream.lock`](upstream.lock)** pins the exact upstream repo + commit SHA
  and lists every symbol we depend on with its expected value and what in espjtag
  relies on it.
- **[`scripts/check_upstream.py`](scripts/check_upstream.py)** re-fetches each
  pinned file at the upstream default-branch HEAD, re-extracts *only* those
  symbols, and prints **GO** (nothing we depend on moved), **NO-GO** (a
  depended-on value changed → review the port, exit 1), or **UNKNOWN** (offline /
  rate-limited, exit 2). Run it any time: `python3 scripts/check_upstream.py`.
- **[`.github/workflows/upstream-check.yml`](.github/workflows/upstream-check.yml)**
  runs it weekly (and on demand) and opens an alert issue on NO-GO.

### Why hand-curated, not auto-generated

We **deliberately vendor a hand-curated subset** rather than mechanically
regenerating `constants.py` from `debug_defines.h`. Reasons:

- `debug_defines.h` defines **thousands** of symbols across the whole RISC-V debug
  spec; espjtag uses a few dozen. A full mechanical import would bury our actual
  dependencies in noise and obscure which values matter.
- Several espjtag constants are *derived* (e.g. we store masks as `1 << OFFSET`,
  and `CMD_ACCESS_REGISTER = 0 << 24`), not 1:1 copies of an upstream `#define`,
  so a naive generator would not reproduce `constants.py` anyway.
- The USB-JTAG values come from a `.c` file's macros, not a clean header, so
  there is no single uniform thing to parse across both sources.

A mechanical regenerator is therefore **not** worth building. The
`check_upstream.py` go/no-go check gives the real safety benefit (catching drift
in exactly the values we use) without the downsides of a full vendored import. If
espjtag's coverage grows to need most of `debug_defines.h`, revisit and add a
small parser keyed off the symbol list already in `upstream.lock`.
