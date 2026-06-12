# espjtag docs — the JTAG / esptool / debug-GUI research library

A navigable index of the JTAG-debugger research behind `espjtag`. Most of these
docs were written on the **esp32-zephyr** bench while espjtag was being built, and
moved here so they live with the code they describe. They are detail / history /
design notes; the package [`README.md`](../README.md) is the user-facing entry
point.

> **Status lives in the issue tracker, not in this prose.** These docs describe
> *what was found and designed*; for "is it built / fixed / done", follow the
> linked **[awtoau/espjtag issues](https://github.com/awtoau/espjtag/issues)**.
> Status baked into doc prose drifts — e.g. an earlier audit banner claimed the
> DMI-DMIRESET fix had landed when it had been reverted (now tracked as
> [#25](https://github.com/awtoau/espjtag/issues/25)). When a doc and an issue
> disagree, the issue wins.

## What this body of work is

Three coupled threads, all driven from the esp32-zephyr bench:

1. **`espjtag`** — this repo: a from-scratch, **pure-Python** RISC-V JTAG debugger.
   It drives the **RISC-V Debug Module** over the ESP32's **built-in
   USB-Serial/JTAG** peripheral (`303a:1001`), no OpenOCD binary, ~500 lines of
   pyusb. Today it does IDCODE/DTMCS, DMI read/write, halt/resume/examine, GPR+CSR
   read/write, memory read/write (System Bus Access), `reset_run()` (reboot a
   running core), and `reset_run_from_rom()` (boot a C6 out of post-flash ROM —
   bench-proven 3/3). RISC-V only (C3/C5/C6/H2).
2. **An esptool fork** ([`awtoau/esptool`](https://github.com/awtoau/esptool)) that
   would integrate espjtag's JTAG reset so the **C6 reset-after-flash "just works"**
   over USB-Serial/JTAG without OpenOCD (design — see
   [ESPTOOL-JTAG-INTEGRATION-PLAN.md](ESPTOOL-JTAG-INTEGRATION-PLAN.md)).
3. **A Flutter debug GUI** — a live physical view of a board (per-pin GPIO, regs,
   memory) over WiFi to a host that runs OpenOCD/espjtag (design + geometry
   research — see [FLUTTER-DEBUG-GUI-DESIGN.md](FLUTTER-DEBUG-GUI-DESIGN.md),
   [BOARD-GEOMETRY-RESEARCH.md](BOARD-GEOMETRY-RESEARCH.md)).

Issues live on **[awtoau/espjtag](https://github.com/awtoau/espjtag/issues)** (the
JTAG library + GUI + MCP, no hyphen). Firmware / bench / tooling follow-ups are
tracked here too as the docs/work migrated in [#26](https://github.com/awtoau/espjtag/issues/26).

---

## Every doc at a glance

The two narrative docs are the current through-line; the rest are detail/history
that align to them.

| Doc | Purpose | Issue(s) |
|---|---|---|
| [ESPJTAG-STORY.md](ESPJTAG-STORY.md) | **Narrative.** Pure-Python JTAG replaces OpenOCD; ~235× faster; the landed speed tricks + current numbers | [#8](https://github.com/awtoau/espjtag/issues/8), [#12](https://github.com/awtoau/espjtag/issues/12) |
| [JTAG-BENCHMARK-ANALYSIS.md](JTAG-BENCHMARK-ANALYSIS.md) | The headline numbers (FAIR rebuild): espjtag ~73–202 µs/word, on par with OpenOCD's batched path; probe-rs ~1.7× faster fairly measured | [#8](https://github.com/awtoau/espjtag/issues/8), [#12](https://github.com/awtoau/espjtag/issues/12) |
| [ESPJTAG-VS-OPENOCD-AUDIT.md](ESPJTAG-VS-OPENOCD-AUDIT.md) | Line-by-line correctness+timing audit vs OpenOCD; found 2 correctness bugs. dmi_write-status + drain-off are done in code ([#12](https://github.com/awtoau/espjtag/issues/12)); the DMIRESET-on-busy item is **not** in the code ([#25](https://github.com/awtoau/espjtag/issues/25), open) | [#12](https://github.com/awtoau/espjtag/issues/12), [#25](https://github.com/awtoau/espjtag/issues/25), [#8](https://github.com/awtoau/espjtag/issues/8) |
| [PROBE-RS-STUDY.md](PROBE-RS-STUDY.md) | probe-rs as a 2nd reference impl; confirms our approach; blueprints Xtensa, progbuf, flash-loader, per-chip YAML | [#3](https://github.com/awtoau/espjtag/issues/3), [#4](https://github.com/awtoau/espjtag/issues/4), [#8](https://github.com/awtoau/espjtag/issues/8), [#9](https://github.com/awtoau/espjtag/issues/9) |
| [HARDWARE-JTAG-VS-USB-JTAG.md](HARDWARE-JTAG-VS-USB-JTAG.md) | Desk study: external probe (J-Link/FT2232/BMP) vs built-in USB-JTAG; eFuse caveats (speed numbers are the pre-fix baseline — banner) | [#10](https://github.com/awtoau/espjtag/issues/10), [#8](https://github.com/awtoau/espjtag/issues/8) |
| [ESPTOOL-JTAG-INTEGRATION-PLAN.md](ESPTOOL-JTAG-INTEGRATION-PLAN.md) | Plan to add a `--after jtag-reset` ResetStrategy to esptool; CDC+JTAG coexistence; PR matrix | [#6](https://github.com/awtoau/espjtag/issues/6) |
| [FLUTTER-DEBUG-GUI-DESIGN.md](FLUTTER-DEBUG-GUI-DESIGN.md) | GUI architecture: OpenOCD Tcl RPC, phone-over-WiFi, BoardState model, phases | [#11](https://github.com/awtoau/espjtag/issues/11), [#15](https://github.com/awtoau/espjtag/issues/15) |
| [BOARD-GEOMETRY-RESEARCH.md](BOARD-GEOMETRY-RESEARCH.md) | KiCad→geometry pipeline (kiutils + glTF) for the board viewer; pad↔GPIO join | (GUI track) |
| [OTHER-CORE-DEBUG-PROTOCOLS.md](OTHER-CORE-DEBUG-PROTOCOLS.md) | Xtensa (S2/S3) + Nordic nRF debug; backend strategy (espjtag/pyOCD/OpenOCD/probe-rs) | [#9](https://github.com/awtoau/espjtag/issues/9) |
| [JTAG-MCP-PRIOR-ART.md](JTAG-MCP-PRIOR-ART.md) | Prior art for espjtag's MCP server: who else exposed a JTAG/SWD/GDB debugger as MCP tools | [#14](https://github.com/awtoau/espjtag/issues/14) |
| [GIT-HISTORY-IDEAS.md](GIT-HISTORY-IDEAS.md) | Archaeology of local repos for prior art (Rust USB samples, recovery escalation, GUI patterns) | [#6](https://github.com/awtoau/espjtag/issues/6), [#13](https://github.com/awtoau/espjtag/issues/13) |
| [C6-USJ-RESET.md](C6-USJ-RESET.md) | The C6 post-flash ROM-stick problem; OpenOCD `reset run` + pure-Python `reset_run_from_rom()` fixes | [#2](https://github.com/awtoau/espjtag/issues/2) |
| [JTAG-PRIMER.md](JTAG-PRIMER.md) | JTAG from the wire up — how espjtag works, bottom to top | — |
| [JTAG-SPEED-PATTERNS.md](JTAG-SPEED-PATTERNS.md) | **The generalized optimizations** — 10 transferable speed patterns + the discipline patterns, ordered by leverage; the porting checklist (read this before optimizing or porting) | — |
| [JTAG-FLASH-WRITES.md](JTAG-FLASH-WRITES.md) | Design for high-speed flash-over-JTAG (RAM loader / ROM calls) | [#3](https://github.com/awtoau/espjtag/issues/3), [#4](https://github.com/awtoau/espjtag/issues/4) |
| [CROSS-PLATFORM-USB.md](CROSS-PLATFORM-USB.md) | Portable USB reset (pyusb `dev.reset()`); Win/Linux/mac | [#13](https://github.com/awtoau/espjtag/issues/13) |
| [MCP-SERVER.md](MCP-SERVER.md) | The espjtag MCP server (tools, pinning, safety tiers) + capability table | [#14](https://github.com/awtoau/espjtag/issues/14) |
| [INCREMENTAL-FLASH-DESIGN.md](INCREMENTAL-FLASH-DESIGN.md) | Incremental-flash engine design + prior-art survey (esptool/probe-rs/ST/pyOCD/SEGGER) | [#3](https://github.com/awtoau/espjtag/issues/3) |
| [CUBEPROGRAMMER-BUGS.md](CUBEPROGRAMMER-BUGS.md) | Two CubeProgrammer `incremental` bugs found on bench (additive-sum skip; two-probe SIGSEGV) | — |
| [PYOCD-INCREMENTAL-PROOF.md](PYOCD-INCREMENTAL-PROOF.md) | Silicon proof: pyOCD default incremental is correct on ST-Link (catches ST's sum-collision). Its `fast_program` data-loss bug: stub only — pursued outside this repo (full analysis in git history @ bcd8a2d) | — |
| [EQUIVALENCE-AND-BENCHMARKS.md](EQUIVALENCE-AND-BENCHMARKS.md) | **Session snapshot**: 3-way equivalence (espjtag=OpenOCD=probe-rs on every board) + transport/flash benchmarks (espjtag leads bulk reads, fastest flasher 256 ms) | [#8](https://github.com/awtoau/espjtag/issues/8) |
| [FLASH-DIE-SURVEY.md](FLASH-DIE-SURVEY.md) | Fleet flash dies (4 vendors!) + measured per-vendor erase/program timings (2.6× erase spread = the cross-board bench variance) + how chips discover memories (RDID/SFDP/IDF drivers/ROM geometry) | — |

> The firmware/bench side of this story — the fleet, per-chip flashing, and *why*
> the C6 reset mess pulled in JTAG/OpenOCD — is summarized in
> [C6-USJ-RESET.md](C6-USJ-RESET.md) and [ESPJTAG-STORY.md](ESPJTAG-STORY.md).

---

## Headline facts (the gist without reading everything)

- **espjtag does real RISC-V debug in pure Python.** Halt/resume, GPR+CSR
  read/write, memory read/write (SBA), reset — over the built-in USB-JTAG, no
  OpenOCD. (package `README.md` "What works"; OTHER-CORE-DEBUG-PROTOCOLS.md.)
- **Speed went ~1,200× and now LEADS the field.** ~24,456 → **~20–23 µs/word** bulk
  reads (probe-rs 46, OpenOCD 27–97), writes ~30 (OpenOCD 27 — tied), single-word
  read 247 µs (probe-rs parity). The eras: drain fix → batched SBA → full-rate TCK
  (a hardcoded div=20 had capped TCK at 1.2 MHz) → async IN/OUT streaming +
  capture-narrowing + OUT memoization. Incremental flash: **246 ms** for a 64 KiB
  2-sector update via the #27 resident RAM stub — fastest tool on the bench.
  (JTAG-BENCHMARK-ANALYSIS.md and ESPJTAG-STORY.md carry the history, with
  supersession banners; live numbers = the package README + the run DBs.)
- **The C6 ROM-boot works in pure Python** — but only as a **combination**:
  USBDEVFS reset (clear the download-strap latch) **+** the full ndmreset / SBA
  soc-reset / deassert / halt / resume handshake. A bare `ndmreset` alone is **not**
  enough (verified 0/3). (GIT-HISTORY-IDEAS.md §3b; C6-USJ-RESET.md.)
- **probe-rs and OpenOCD cross-confirm our approach.** Independent ports landed on
  the **byte-identical** C6 reset sequence; both **never drain** the IN endpoint
  (precise byte-accounting); both treat TCK speed as irrelevant at USB-Full-Speed.
  (PROBE-RS-STUDY.md; ESPJTAG-VS-OPENOCD-AUDIT.md.)
- **The bottleneck is USB round-trips, not TCK, not the bus.** USB Full-Speed
  (12 Mbps) ≈ 2 µs/byte on the wire; the levers are *fewer round-trips* (batch),
  *overlap* (async), and *shrink the OUT* (RLE). (perf tracker
  [#8](https://github.com/awtoau/espjtag/issues/8).)

---

## The tracks

For per-track status — what's *built* vs *designed* vs *researched* — read the
linked issues; bullets here describe the work, not its live state.

### Track A — espjtag debugger + speed  ·  [#8](https://github.com/awtoau/espjtag/issues/8), [#12](https://github.com/awtoau/espjtag/issues/12), [#11](https://github.com/awtoau/espjtag/issues/11), [#4](https://github.com/awtoau/espjtag/issues/4)

The core debugger works on all RISC-V parts (C3/C5/C6/H2). Speed went ~235× via
landed changes — **drain removal** and **batched SBA reads** — and is now on par
with OpenOCD's batched path in pure Python (JTAG-BENCHMARK-ANALYSIS.md). The
follow-on speed work is mostly measured-but-not-yet-landed: async USB → 2×
([#19](https://github.com/awtoau/espjtag/issues/19)), RLE on the OUT → 3.4×
([#20](https://github.com/awtoau/espjtag/issues/20)), `int.from_bytes` unpack
([#21](https://github.com/awtoau/espjtag/issues/21)), batched writes
([#22](https://github.com/awtoau/espjtag/issues/22)), FIFO 480→1024
([#23](https://github.com/awtoau/espjtag/issues/23)). The **audit**
(ESPJTAG-VS-OPENOCD-AUDIT.md) found two correctness bugs; the `dmi_read`
raise-on-exhaustion and `dmi_write` op-status retry are in the code
([#12](https://github.com/awtoau/espjtag/issues/12)), but the **DTMCS DMIRESET on
busy is not** — tracked as [#25](https://github.com/awtoau/espjtag/issues/25).
[#11](https://github.com/awtoau/espjtag/issues/11) ("JTAG verifies what esptool
can't") is the product motivation; [#24](https://github.com/awtoau/espjtag/issues/24)
is the WDT-disable-on-halt fix for C6 halt flakiness.

### Track B — esptool integration  ·  [#6](https://github.com/awtoau/espjtag/issues/6)

The plan (ESPTOOL-JTAG-INTEGRATION-PLAN.md) is a minimal upstream-PR-able boundary:
a new `JTAGSystemReset` ResetStrategy + a `--after jtag-reset` choice + a
C3/C6/C5/H2 target override, fixing esptool #970 (the C6 has no working `--after`
over USB-JTAG). **CDC + JTAG-vendor-interface coexistence** on the same device is
confirmed feasible. The decisive bench finding: **bare ndmreset does NOT boot a C6
from post-flash ROM** — so the PR's reset class must carry OpenOCD's *deeper*
sequence, not just the pulse.

### Track C — the GUI + board viz  ·  [#11](https://github.com/awtoau/espjtag/issues/11), [#14](https://github.com/awtoau/espjtag/issues/14)–[#16](https://github.com/awtoau/espjtag/issues/16), [#18](https://github.com/awtoau/espjtag/issues/18)

The architecture (FLUTTER-DEBUG-GUI-DESIGN.md): the **phone never touches USB** — it
talks TCP/WiFi to a host that runs **OpenOCD's Tcl RPC** (`:6666`), and everything
the GUI needs reduces to **halt/resume + read-register + read-memory**. Ship
**Shape A** (Flutter → OpenOCD-Tcl directly) first, then a **Shape B** adapter that
can swap the backend to espjtag. The geometry side (BOARD-GEOMETRY-RESEARCH.md)
recommends **kiutils** to emit `board-geometry.json` from vendored KiCad, joined to
the live GPIO bit via the existing pad↔GPIO map (tracked for migration in
[#26](https://github.com/awtoau/espjtag/issues/26)).
The MCP server (MCP-SERVER.md, [#14](https://github.com/awtoau/espjtag/issues/14))
exposes the debugger as AI tools.

### Track D — other cores (Xtensa / Nordic)  ·  [#9](https://github.com/awtoau/espjtag/issues/9)

OTHER-CORE-DEBUG-PROTOCOLS.md and PROBE-RS-STUDY.md §4 map the two non-RISC-V
architectures. **Xtensa (S2/S3)** uses the **identical USB transport** espjtag
already speaks, but a wholly different **Xtensa OCD/XDM** debug module; probe-rs's
`xdm.rs` is a ready blueprint. **Recommendation: route S2/S3 through OpenOCD or
probe-rs unless the no-dependency story matters.** **Nordic nRF** shares nothing
(ARM CoreSight/SWD via a CMSIS-DAP probe) — **use pyOCD**.

---

## Where the open work lives (issue map)

The plan is the **issue tracker**, not this index — bullets go stale, issues don't.

| Track | Issues | Detail doc(s) |
|---|---|---|
| espjtag speed (perf label) | [#8](https://github.com/awtoau/espjtag/issues/8) (umbrella) · async [#19](https://github.com/awtoau/espjtag/issues/19) · RLE [#20](https://github.com/awtoau/espjtag/issues/20) · unpack [#21](https://github.com/awtoau/espjtag/issues/21) · batched writes [#22](https://github.com/awtoau/espjtag/issues/22) · FIFO 480→1024 [#23](https://github.com/awtoau/espjtag/issues/23) · WDT-disable on halt [#24](https://github.com/awtoau/espjtag/issues/24) | JTAG-BENCHMARK-ANALYSIS |
| espjtag correctness / timing | [#12](https://github.com/awtoau/espjtag/issues/12) (never-close audit) · DMIRESET-on-busy [#25](https://github.com/awtoau/espjtag/issues/25) · [#11](https://github.com/awtoau/espjtag/issues/11) (verify what esptool can't) | ESPJTAG-VS-OPENOCD-AUDIT |
| esptool integration | [#6](https://github.com/awtoau/espjtag/issues/6) | ESPTOOL-JTAG-INTEGRATION-PLAN |
| per-chip data + flash-over-JTAG | [#4](https://github.com/awtoau/espjtag/issues/4) (config table) · [#3](https://github.com/awtoau/espjtag/issues/3) (flash-over-JTAG) | PROBE-RS-STUDY, JTAG-FLASH-WRITES |
| GUI + MCP + board viz | [#14](https://github.com/awtoau/espjtag/issues/14)–[#16](https://github.com/awtoau/espjtag/issues/16), [#18](https://github.com/awtoau/espjtag/issues/18), [#11](https://github.com/awtoau/espjtag/issues/11) | FLUTTER-DEBUG-GUI-DESIGN, BOARD-GEOMETRY-RESEARCH, JTAG-MCP-PRIOR-ART, MCP-SERVER |
| other cores (Xtensa / Nordic) | [#9](https://github.com/awtoau/espjtag/issues/9) | OTHER-CORE-DEBUG-PROTOCOLS |
| cross-platform + recovery | [#13](https://github.com/awtoau/espjtag/issues/13) (portable USB reset) · [#6](https://github.com/awtoau/espjtag/issues/6) (recovery escalation) | GIT-HISTORY-IDEAS, CROSS-PLATFORM-USB |
| external hardware JTAG comparison | [#10](https://github.com/awtoau/espjtag/issues/10) | HARDWARE-JTAG-VS-USB-JTAG |
