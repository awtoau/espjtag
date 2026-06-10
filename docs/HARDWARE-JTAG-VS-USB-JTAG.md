# External hardware JTAG vs the ESP32-C6 built-in USB-Serial/JTAG

Research + informed speculation for a GitHub issue. **No hardware action** — this is a
desk study of whether driving the C6's RISC-V Debug Module through an *external* JTAG
probe on the physical TCK/TMS/TDI/TDO pins would be faster or more robust than our
current path (the pure-Python `espjtag` client over the chip's **built-in**
USB-Serial/JTAG, `303a:1001`).

Throughout, lines are tagged **[FACT]** (sourced, with a URL) or **[SPECULATION]**
(reasoned guess — the user explicitly invited it). Sources are listed at the bottom and
linked inline. Research current as of 2026-06-10.

> **Speed numbers below are the PRE-FIX baseline — superseded.** The "~22 ms/DMI-read,
> ~24.5 ms/word, ~20 ms `_drain_in` timeout" figures in §3/§5 describe espjtag *before*
> the drain fix. The Phase-0 prediction this doc makes ("most of the 22 ms is the drain;
> fix software first") **was confirmed**: the drain is removed and single ops are now
> ~200–330 µs, memory reads ~73–202 µs/word — see
> **[JTAG-BENCHMARK-ANALYSIS.md](JTAG-BENCHMARK-ANALYSIS.md)** and
> **[ESPJTAG-STORY.md](ESPJTAG-STORY.md)** for current figures. The *conclusions* of this
> doc still hold — batching is a software property, and a hardware probe's edge over
> batched USB-JTAG is smaller than intuition suggests — only the headline numbers are
> historical. The eFuse / external-probe / wedge-recovery analysis (§1, §2, §4, §6) is
> unaffected and current.

---

## TL;DR

- **[FACT]** The C6 *can* route JTAG to GPIO pins for an external probe:
  **GPIO4=MTMS, GPIO5=MTDI, GPIO6=MTCK, GPIO7=MTDO**. Switching to pin-JTAG needs an
  **irreversible eFuse burn** (`DIS_USB_JTAG`, or `JTAG_SEL_ENABLE` + the GPIO15 strap).
- **[FACT]** **Our XIAO ESP32-C6 does not break GPIO4–7 out to any pad.** The XIAO
  connector exposes GPIO 0,1,2,21,22,23,16,17,19,20,18 — none of the four JTAG pins.
  So on *this* board, external pin-JTAG isn't wireable without soldering to the module.
- **[FACT]** **Black Magic Probe**: RISC-V support exists in BMD ≥ v2.0.0, but
  **ESP32-C3 was merged "untested"** and flashing/memory had known gaps; **no working
  ESP32-C6 reports found.** Treat BMP-on-ESP-RISC-V as *unproven*.
- **[FACT]** **SEGGER J-Link** added **official** ESP32-C2/C3/**C6**/H2 RISC-V support in
  **v8.10 (Oct 2024)** — that's the credible external-probe option, alongside FT2232H +
  Espressif's OpenOCD.
- **[FACT + SPECULATION]** Our measured built-in baseline *at the time of writing* was
  **~22 ms per DMI read, ~24.5 ms per memory word** — and that is **USB round-trip
  latency**, not TCK speed. *(Superseded: that was pre-fix; the ~20 ms was a drain
  timeout, now removed — current is ~73–202 µs/word. See the banner above.)* An external
  probe clocking TCK at MHz and **batching** scans should crush bulk memory dumps; for
  single pokes (reset) it won't matter. **But** the same batching could be done over the
  built-in USB-JTAG too — so the honest framing is *our client was un-batched*, not
  *USB-JTAG is intrinsically slow*. (Confirmed: batched espjtag now matches OpenOCD over
  the same transport.)

---

## 1. Does the C6 expose external pin-JTAG? Which GPIOs? What eFuse?

**[FACT]** By default the C6's JTAG TAP is wired internally to the USB_SERIAL_JTAG
peripheral (the `303a:1001` device we already use). To drive JTAG from an external
probe you switch the TAP to GPIOs. From the ESP-IDF C6 *Configure Other JTAG Interfaces*
guide, the pin map is:

| C6 signal | GPIO | JTAG |
|-----------|------|------|
| MTMS      | GPIO4 | TMS |
| MTDI      | GPIO5 | TDI |
| MTCK      | GPIO6 | TCK |
| MTDO      | GPIO7 | TDO |

Two eFuse routes, both **irreversible** (eFuses are one-time-programmable):

- **`DIS_USB_JTAG`** — *permanently* disables the internal link between
  USB_SERIAL_JTAG and the JTAG TAP. JTAG then lives only on GPIO4–7. **[FACT]** The USB
  *CDC* (serial) half of the peripheral still works, so flashing/monitor over USB CDC
  survives the burn — you lose USB-*JTAG*, not USB-*serial*.
- **`JTAG_SEL_ENABLE`** — enables a runtime selector on **strapping pin GPIO15**: GPIO15
  **low at reset → pin-JTAG (GPIO4–7)**; GPIO15 **high at reset → USB-JTAG**. This is the
  non-destructive-*ish* option (you keep both, chosen per-boot by the strap) — but the
  enable itself is still a permanent eFuse burn.

**[FACT]** Espressif's docs state plainly: *"Burning eFuses is an irreversible
operation, so please consider the above option before starting the process."* You cannot
use USB-JTAG and pin-JTAG **simultaneously**; the TAP is one resource routed one way at a
time. **[FACT]** GPIO4 and GPIO5 (MTMS/MTDI) are also among the C6 strapping pins, so
they carry boot-time meaning if you wire a probe to them.

> ⚠️ **IRREVERSIBLE-eFUSE WARNING (loud, on purpose).** `DIS_USB_JTAG` and
> `JTAG_SEL_ENABLE` are one-way. There is no un-burn. If you burn `DIS_USB_JTAG` you have
> **permanently lost the built-in USB-JTAG on that physical chip** — and therefore our
> entire `espjtag`/OpenOCD-over-USB flow on it, forever. Do this only on a *sacrificial*
> board you're willing to brick for the experiment, never on a daily-driver.

### Pin availability on our XIAO C6 — the practical blocker

**[FACT]** From our own board pinout (`docs/XIAO-PINOUT.md`, generated from the Zephyr
DT), the XIAO ESP32-C6 pads map to these GPIOs:

```
D0=GPIO0  D1=GPIO1  D2=GPIO2  D3=GPIO21 D4=GPIO22 D5=GPIO23
D6=GPIO16 D7=GPIO17 D8=GPIO19 D9=GPIO20 D10=GPIO18
```

**None of GPIO4, 5, 6, 7 (the JTAG pins) is on a XIAO pad.** GPIO15 (the JTAG_SEL strap)
is likewise not broken out. So on the XIAO C6 specifically:

- **[FACT]** You cannot wire an external probe to JTAG without soldering directly to the
  ESP32-C6 module's castellations/pins for GPIO4–7 — fiddly 0.1"-incompatible work.
- **[SPECULATION]** A board that *does* expose GPIO4–7 (e.g. the ESP32-C6-DevKitC-1,
  which brings most GPIOs to headers) would be the right vehicle for this experiment,
  not the XIAO. **The XIAO is the wrong board for hardware-JTAG bring-up.**

---

## 2. Black Magic Probe + ESP32 RISC-V — honest status

**Short answer: don't count on BMP for the C6.**

- **[FACT]** BMP/Black Magic Debug describes itself as *"in-application debugger for ARM
  Cortex and RISC-V processors"* — RISC-V is in scope, and **v2.0.0 (Jul 2025)** shipped
  real RISC-V Debug-spec v0.13 support, System-Bus memory access, JTAG bit-bang
  improvements, and GD32VF103 (a RISC-V part) as a *working* target.
- **[FACT]** ESP32-C3 specifically: the original RISC-V work (PR #924, "Riscv 0.13
  Working on GD32VF103, debug working on ESP32-C3") was **WIP and never merged** — closed
  Feb 2023, superseded by later work. Its own notes flagged *"ESP32-C3 memory setup, help
  needed"* and *"ESP32-C3 flashing: Probably needs major effort,"* plus a reported
  *"target hangs if debugger writes a single byte"* bug. The community summary that
  "JTAG support for RISC-V ESP32-C3 was merged" is consistently followed by **"but not
  tested."**
- **[FACT]** **No working ESP32-C6 + BMP report was found** in this research. The
  ESP32-C6 is newer than the C3 and isn't called out in BMD target docs.
- **[SPECULATION]** The C6 core is a standard RV32 with a RISC-V Debug-Module v0.13 (same
  family our `espjtag` already drives), so BMD's generic RISC-V path *might* enumerate it
  over physical JTAG. But Espressif parts have quirks (the abstract-command/SBA flavour,
  reset behaviour, the strap-relatch issue we already hit) that BMD's generic driver
  hasn't been hardened against. **Plausible it half-works; unsafe to assume it does.**

**[FACT]** Note also `Ebiroll/esp32_blackmagic` — that's BMP *firmware running on* an
ESP32 to debug **ARM** targets. It is the **opposite** of what we want (ESP32 as the
probe, not the target) and is irrelevant here. Easy to trip over in searches.

### If not BMP, then what? (the real external options)

- **[FACT]** **SEGGER J-Link** added **official** support for Espressif RISC-V parts —
  ESP32-C2/C3/**C6**/H2 — in **J-Link software v8.10 (Oct 2024)**. This is the
  *supported, vendor-blessed* external probe. Works with J-Link's own RISC-V path and is
  OpenOCD-compatible.
- **[FACT]** **FT2232H + Espressif's `openocd-esp32`** is the classic external-JTAG route
  for ESP32 (e.g. ESP-Prog, ESP-WROVER-KIT's onboard FT2232H at up to 20 MHz TCK). It's
  a generic MPSSE JTAG adapter; OpenOCD knows the ESP RISC-V target.
- **[SPECULATION]** So the *honest* comparison for a C6 isn't "BMP vs USB-JTAG" — it's
  **"J-Link (or FT2232H) + OpenOCD on the GPIO4–7 pins vs the built-in USB-JTAG."** BMP
  is a maybe-future, not a today-tool, for this chip.

---

## 3. Speed: built-in USB-JTAG vs external hardware JTAG (the core question)

### Our measured built-in baseline — [FACT]

From our own test DB (`tmp/jtag-tests.db`, `scripts/jtag_testdb.py`), on the XIAO C6 over
the built-in USB-JTAG with `espjtag`:

| Metric | Measured (C6) |
|--------|---------------|
| `dmiread_us` — mean DMI register read | **~22.4 ms/op** (22357–22542 µs, n=9) |
| `memread_us` — memory word via System-Bus Access | **~24.5 ms/word** (24447–24641 µs) |
| `examine_ms` — open + read IDCODE | **~26 ms** (23–46 ms) |

### Why it's that slow — [FACT] (from our own transport code)

It is **not** TCK speed. Reading `vendor/espjtag/espjtag/transport.py`, a single
`dmi_read` does: drain IN → reset TAP → IR-select DMI → scan READ → idle → scan NOP →
idle → **one `ep_out.write()` (one bulk OUT) then one `ep_in.read()` (one bulk IN)**. So
each DMI op = **exactly one USB request/response round-trip**. ~22 ms is dominated by
that round-trip plus per-call Python/pyusb/libusb/kernel overhead and the `_drain_in()`
read that waits out a 20 ms timeout when the endpoint is already empty.

**[SPECULATION]** That 20 ms `_drain_in()` timeout is almost certainly *most* of the
22 ms — it's suspiciously close to the measured per-op time. The actual JTAG-over-USB
scan is hundreds of µs; we're paying a fixed ~20 ms "is the IN endpoint empty?" tax on
every single op. **If true, the biggest speedup available to us isn't hardware JTAG at
all — it's removing/shortening that drain.** Worth measuring before buying any probe.

**[FACT]** Independently, Espressif's OpenOCD treats the built-in `esp_usb_jtag` as a
~40 MHz "base speed" adapter whose TCK divisor **can't actually be changed** from
OpenOCD (issues OCD-633 #248, OCD-1001 #337). So the silicon clocks JTAG fast; the
ceiling is the **USB full-speed 12 Mbps link and per-transaction latency**, not TCK.

### External hardware JTAG — [FACT + SPECULATION]

- **[FACT]** An FT2232H MPSSE clocks TCK up to ~30 MHz (ESP-WROVER-KIT runs its onboard
  FT2232H at 20 MHz, "difficult to achieve with an external adapter"). J-Link similarly
  drives multi-MHz TCK. Both **buffer and batch** many scans into one USB transfer.
- **[FACT]** Real-world ESP JTAG throughput numbers are mediocre in practice, though:
  built-in USB-JTAG flash programming has been measured around **~123 KB/s**, and older
  FT2232H OpenOCD setups around **~30 KB/s** — and there are many reports of ESP-Prog
  (FT2232H) debugging being *"extremely slow"* (OCD-288 #136). So external ≠
  automatically fast; it depends heavily on OpenOCD's algorithm and the work-area setup.
- **[SPECULATION]** Where each wins:
  - **Single-register pokes (reset, halt, one DMI write):** built-in USB-JTAG is fine.
    One round-trip either way; a hardware probe saves nothing meaningful. Our reset path
    (`espjtag reset_run`) has no reason to move to hardware JTAG.
  - **Bulk memory dump / fast flashing / the GUI's GPIO polling:** a probe that batches
    N scans per USB transfer should beat *our un-batched* client by 1–2 orders of
    magnitude, because the win is **amortising USB latency over many scans**, not faster
    TCK. If we dump 1024 words at 22 ms each that's ~22 s; a batched probe doing the same
    in a handful of USB transfers is sub-second.
  - **The subtle point:** that batching is a *software* property, not a *hardware* one.
    The built-in USB-JTAG can also carry many scans per bulk transfer (OpenOCD already
    does this over `esp_usb_jtag`). **So an apples-to-apples "is hardware JTAG faster?"
    needs OpenOCD-over-USB-JTAG as the middle data point**, or we'll wrongly credit the
    probe for a speedup that's really just batching.

**[SPECULATION] Rough expectation:** for a 4 KB memory dump — our current client ≈
**24.5 ms × 1024 ≈ 25 s**; OpenOCD over the *same* built-in USB-JTAG ≈ **a few seconds**
(batched, ~100 KB/s class); J-Link/FT2232H on pins ≈ **similar few seconds, maybe
faster** if its work-area/algorithm is better. The dramatic jump is batching; the
probe's incremental edge over batched-USB-JTAG is real but smaller than naive intuition
says.

---

## 4. Why would we even want hardware JTAG?

Three distinct motivations — only one is purely about speed:

1. **[FACT/SPECULATION] Speed for bulk ops** — memory dumps, fast flashing, the Flutter
   GUI's GPIO/register polling. As above, much of the win is batching (achievable over
   USB-JTAG too); the probe is the *guaranteed-fast* version. Real, but partly addressable
   in software first.
2. **[FACT] Wedge recovery — the failure mode we actually hit.** We have documented cases
   where the built-in USB-Serial/JTAG **stops responding from a running app** — the USJ
   write-timeout (`MEMORY.md`: "USJ flash Write timeout", recovery via `usbreset`/replug)
   and the C6 post-flash ROM-download stick (`docs/C6-USJ-RESET.md`). An **independent
   external probe on the JTAG pins doesn't share the USB peripheral**, so it can attach to
   the Debug Module even when the on-chip USB side is busy/wedged. This is arguably the
   *strongest* reason — it's a different physical path to the same TAP.
   - **[SPECULATION]** Caveat: many of our wedges are *strap/reset* problems, not
     TAP-access problems. A probe gets you DM access but may still not re-sample the BOOT
     strap (that needs a full-system reset). So hardware JTAG helps "can't reach the DM"
     wedges more than "stuck in ROM download" ones.
3. **[FACT] Ground-truth validation of `espjtag`.** A second, independent debugger
   (J-Link/OpenOCD) reading the same IDCODE/DMSTATUS/registers/memory gives us a
   **reference oracle** to validate our pure-Python USB-JTAG client against. If `espjtag`
   and J-Link disagree on a register read, that's a bug in our TAP/DMI logic. High value
   for trust in the home-grown stack, independent of speed.

---

## 5. Concrete test plan — tracked in espjtag #10

The phased test plan that used to live here (Phase 0 software-only drain/batch fix →
Phase 1 J-Link/FT2232H on a C6-DevKitC-1's GPIO4–7 → Phase 2 ground-truth diff →
Phase 3 BMP stretch), with the full wiring, eFuse-burn warning, and
`jtag_testdb.py`/`jtag-tests.db` measurement protocol, now lives in the issue it was
written for:

> **[awtoau/espjtag#10](https://github.com/awtoau/espjtag/issues/10)** — *Compare
> external hardware JTAG (J-Link/FT2232/BMP) vs the built-in USB-JTAG.* The do-first
> step is **Phase 0: software fixes (drain + batch, espjtag #8) before buying any
> probe** — likely recovers most of the apparent "slowness." The detail-doc here keeps
> the research narrative (§1–§4, §6 and the sources) that the issue summarises.

---

## 6. Risks (read before buying anything)

- **[FACT] Irreversible eFuse.** `DIS_USB_JTAG` / `JTAG_SEL_ENABLE` cannot be undone.
  `DIS_USB_JTAG` *permanently* kills built-in USB-JTAG (and our whole USB debug flow) on
  that physical chip. Sacrificial board only.
- **[FACT] Losing USB-JTAG / strap entanglement.** Pin-JTAG and USB-JTAG are mutually
  exclusive at any instant. GPIO4 (MTMS) and GPIO5 (MTDI) are also boot strapping pins —
  a probe holding them at reset can change boot behaviour.
- **[FACT] Pin availability on the XIAO.** GPIO4–7 and GPIO15 are **not** on XIAO C6
  pads. The XIAO is the wrong board for this; use a DevKit that headers those GPIOs, or
  resign yourself to micro-soldering on the module.
- **[FACT] BMP is unproven on ESP RISC-V** — especially the C6. Don't make it the primary
  plan; J-Link (official) or FT2232H+OpenOCD are the dependable external paths.
- **[SPECULATION] The probe may not be the win we imagine.** If Phase-0 shows our 22 ms is
  mostly the `_drain_in` timeout and per-call overhead, the right fix is software
  (batch scans / shorten the drain over the existing USB-JTAG), and the external-probe
  speed argument largely evaporates — leaving **wedge-recovery and ground-truth
  validation** as the real reasons to own a J-Link, not raw speed.

---

## Sources

ESP32-C6 pin-JTAG / eFuse:
- ESP-IDF — *Configure Other JTAG Interfaces (ESP32-C6)*:
  <https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-guides/jtag-debugging/configure-other-jtag.html>
- ESP-IDF — *USB Serial/JTAG Controller Console (ESP32-C6)*:
  <https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-guides/usb-serial-jtag-console.html>
- esptool — *Burn eFuse* (irreversible warning, BURN confirm):
  <https://docs.espressif.com/projects/esptool/en/latest/esp32/espefuse/burn-efuse-cmd.html>
- ESP32 strapping pins (C6 GPIO4/5/15 etc.):
  <https://www.espboards.dev/blog/esp32-strapping-pins/>

XIAO C6 pinout (GPIO4–7 not broken out): our `docs/XIAO-PINOUT.md` (generated from the
Zephyr DT); cross-checked against the Zephyr board doc
<https://docs.zephyrproject.org/latest/boards/seeed/xiao_esp32c6/doc/index.html>.

Black Magic Probe / RISC-V / ESP32-C3:
- Black Magic Debug repo (ARM + RISC-V): <https://github.com/blackmagic-debug/blackmagic>
- Supported targets: <https://black-magic.org/supported-targets.html>
- v2.0.0 release (RISC-V Debug v0.13, SBA, GD32VF103), Jul 2025:
  <https://black-magic.org/blog/2025-07-19-bmd-release-v2_0_0.html>
- PR #924 "Riscv 0.13 ... debug working on ESP32-C3" (WIP, not merged; C3 memory/flash
  gaps): <https://github.com/blackmagic-debug/blackmagic/pull/924>
- `Ebiroll/esp32_blackmagic` (BMP *on* ESP32 to debug ARM — the opposite use case):
  <https://github.com/Ebiroll/esp32_blackmagic>

SEGGER J-Link ESP RISC-V support (C2/C3/C6/H2, v8.10, Oct 2024):
- <https://www.segger.com/news/241010-j-link-esp32/>

Speed / adapter behaviour:
- esp_usb_jtag adapter-speed is fixed/unchangeable (OCD-633): <https://github.com/espressif/openocd-esp32/issues/248>
- Can't change JTAG adapter clock (OCD-1001): <https://github.com/espressif/openocd-esp32/issues/337>
- ESP-Prog (FT2232H) OpenOCD "extremely slow" (OCD-288): <https://github.com/espressif/openocd-esp32/issues/136>
- JTAG flash programming speed (OCD-665, ~KB/s class): <https://github.com/espressif/openocd-esp32/issues/259>
- FT2232 + OpenOCD ESP32 setup / TCK rates: <https://mcuoneclipse.com/2019/10/20/jtag-debugging-the-esp32-with-ft2232-and-openocd/>
- J-Link + OpenOCD ESP32 setup / TCK rates: <https://mcuoneclipse.com/2019/09/22/eclipse-jtag-debugging-the-esp32-with-a-segger-j-link/>
- ESP-IDF JTAG debugging (built-in vs external overview):
  <https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-guides/jtag-debugging/index.html>

Our own measurements & code:
- `tmp/jtag-tests.db` via `scripts/jtag_testdb.py` — C6: dmiread ~22.4 ms, memread
  ~24.5 ms/word, examine ~26 ms.
- `vendor/espjtag/espjtag/transport.py` — one USB OUT + one IN per DMI op (latency-bound);
  the `_drain_in()` 20 ms timeout per op.
