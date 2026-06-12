# espjtag — the story: pure-Python JTAG that caught (and in places beat) the giants

> **⚠️ SPEED NUMBERS SUPERSEDED (2026-06-12).** This doc records the state of an
> earlier optimization era. After the TCK-divider fix (a hardcoded div=20 had
> capped TCK at 1.2 MHz), async IN/OUT streaming, capture-narrowing, OUT
> memoization and the #27 RAM stub, the current numbers are: bulk reads
> **20–23 µs/word** (probe-rs 46, OpenOCD 27–97), writes ~30, single-word read
> 247 µs, 64 KiB incremental flash **246 ms**. espjtag now LEADS probe-rs and
> OpenOCD on reads and ties OpenOCD on writes. Live numbers: the README table +
> the recorded run DBs (`jtag-tests.db`, `tmp/flash-bench.db`). The analysis
> and methodology below remain valid history.

How a "we just need to reboot the C6 after flashing" annoyance turned into a
pure-Python RISC-V JTAG debugger that does halt/registers/memory/reset with **no
OpenOCD binary**, got ~235× faster through a bag of tricks, and ended up *faster
than OpenOCD's per-word path* — while staying a few-hundred-lines pyusb script.

> Context: why we needed JTAG at all is summarized in
> [C6-USJ-RESET.md](C6-USJ-RESET.md)
> (the Espressif post-flash-reset bug). This doc is the espjtag side: how it works,
> how it improved, and every speed trick used.

## How it works (in one breath)

The ESP32-C3/C5/C6/H2 expose a USB-Serial/**JTAG** peripheral on their native USB
(`303a:1001`). JTAG is, at bottom, **one long shift register** clocked one bit per
TCK; TMS walks a 16-state machine that decides whether you're loading an instruction
(IR) or data (DR). espjtag:

1. Speaks the Espressif `esp_usb_jtag` USB protocol directly via **pyusb** — packs
   4-bit command nibbles (clock + TMS + TDI) into bulk-OUT transfers, reads captured
   TDO from bulk-IN.
2. Drives the **RISC-V Debug Module** over that: select DMI via IR, scan
   address/data/op, and you can read/write the Debug Module registers — which gives
   halt/resume, GPR+CSR access (abstract command), and memory (System Bus Access).
3. The reset recipe that fixes the Espressif boot bug: USB bus-reset to clear the
   BOOT-strap latch, SoC reset-register writes, then the halt → dcsr → resume
   handshake — all in Python.

That's the whole thing. No FTDI adapter, no OpenOCD config tree — just the cable.

## The improvement story (~235× faster, in pure Python)

We benchmarked memory reads (per 32-bit word, ESP32-C6) across every change, into a
SQLite DB with graphs (`scripts/jtag_testdb.py`, see
[JTAG-BENCHMARK-ANALYSIS.md](JTAG-BENCHMARK-ANALYSIS.md)):

| stage | µs/word | what changed |
|---|---|---|
| baseline | 24,456 | the first naïve client |
| drain fix | 3,658 | **6.7×** — stopped a hidden ~3 ms-per-op wait |
| batched reads | 494 | **7.4×** — N reads in one transaction |
| drain removed | 202 | **2.5×** — the wait was unnecessary entirely |
| (rebuild on ROM) | ~104 | same code, cleaner measurement |

The finish line: **fairly measured, probe-rs (Rust) is ~44 µs/word — still
~1.7× faster than espjtag's ~73 µs/word bulk read.** espjtag is a *close, respectable
second* and **faster than OpenOCD's per-word `mdw` loop** — all in pure Python.
(The earlier "58× faster than probe-rs" claim was a measurement artifact — comparing
our warm client to probe-rs's cold per-call CLI; corrected and documented.)

## The bag of tricks (how you go fast in pure Python over USB)

The one insight under everything: the ESP USB-JTAG is **USB 2.0 Full Speed — 1 ms
frames, no microframes**. So the cost of any operation is **the number of USB
round-trips**, not bits or TCK speed. Every trick below cuts round-trips, hides
them, or stops wasting them.

1. **Don't wait when you don't have to (the drain fix).** The original client did a
   "clear any stale bytes" read before every op with a 1 ms timeout. We *measured*
   (via `espjtag/timing.py`, perf_counter_ns on every transfer) that libusb floors
   an empty read at **~3 ms**, not 1 ms — so this cost ~3 ms *every op*, the single
   biggest hidden cost. Invisible until instrumented. **Lesson: instrument, never
   assume timing.**

2. **The drain was unnecessary at all.** `_recv` reads the *exact* captured byte
   count, so the endpoint is already empty — OpenOCD and probe-rs both confirm by
   *never* draining (they track pending bits precisely). We removed it, keeping a
   `validate` mode that periodically asserts the endpoint really is empty (catches
   any byte-accounting bug loudly) — which then *caught a real bug* in a later
   change. **Lesson: be deterministic, not defensively wasteful — but keep a
   self-check.**

3. **Batch the scans.** A memory read of N words was N separate round-trips. The TAP
   can stay in IR=DMI across consecutive DR scans (no re-select), so you queue N
   scans behind **one** IR-select, flush once, and demux the concatenated capture —
   N round-trips → a few. `read_mem(1024)`: ~25 s → sub-second. The same batching is
   why a 30-register GUI poll is **6.5× cheaper** as one burst than 30 reads.

4. **Respect the tiny IN FIFO.** The device's IN buffer is small; a naïve giant
   batch makes the OUT writes time out once the IN buffers fill. So a batch is
   chunked — send a chunk, drain its captured bytes, continue — like OpenOCD's
   send-loop. (probe-rs's documented limit is 1024 captured bits; we can raise our
   conservative 480.)

5. **Correctness as a first-class speed concern.** A fast *wrong* answer is worse
   than a slow right one. The DMI read now **raises** on a stuck-busy result instead
   of returning the busy garbage as if valid; writes check op-status and retry.
   Found by auditing against OpenOCD + probe-rs line by line
   ([ESPJTAG-VS-OPENOCD-AUDIT.md](ESPJTAG-VS-OPENOCD-AUDIT.md)).

### Tricks identified but not yet landed (the path past OpenOCD/probe-rs)
Measured/estimated and tracked as perf issues under
[espjtag#8](https://github.com/awtoau/espjtag/issues/8):
- **Async/overlapped USB** ([#19](https://github.com/awtoau/espjtag/issues/19),
  python-libusb1, already installed): pipeline the OUT and
  IN transfers (serialized today) → ~2× on single ops, near the irreducible floor.
- **RLE / CMD_REP on the OUT stream** ([#20](https://github.com/awtoau/espjtag/issues/20)):
  a batched read's OUT is ~74% one repeated
  nibble; the protocol has a repeat opcode → shrink the OUT ~3.4× (bursts are
  OUT-bandwidth-bound once the drain's gone).
- **`int.from_bytes` (not numpy) for bit-unpacking**
  ([#21](https://github.com/awtoau/espjtag/issues/21)): the per-bit Python loop in
  `_recv` is the CPU wall for big bursts; vectorize it with stdlib — *no new
  dependency* (we deliberately avoided numpy to stay pyusb-only).
- **Batched WRITES** ([#22](https://github.com/awtoau/espjtag/issues/22)): writes are still per-word (~621 µs/word vs OpenOCD's ~27) —
  the one place espjtag is clearly behind; batching them mirrors the read fix and
  matters for flash-over-JTAG.

## Why it matters

- **Kills the OpenOCD dependency** for flash+boot+debug on the RISC-V parts — just
   the USB cable and Python (see [C6-USJ-RESET.md](C6-USJ-RESET.md)).
- **It's a library**, so it became an esptool reset-fix (`--after jtag-reset`), an
  **MCP server** (debug a chip conversationally from Claude Code), and the backend
  for a planned VS Code extension + Flutter GUI.
- **It's small and readable** — the speed came from understanding the USB cost model,
  not from C or cleverness you can't follow.

Repo: `awtoau/espjtag`. Provenance (ported from openocd-esp32 + the RISC-V debug
spec) in its ACKNOWLEDGEMENTS.md. The data + graphs:
[JTAG-BENCHMARK-ANALYSIS.md](JTAG-BENCHMARK-ANALYSIS.md).
