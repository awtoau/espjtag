# JTAG speed patterns — the generalized optimizations behind espjtag's numbers

The 2026-06-12 campaign took espjtag from 24,456 µs/word to 20 µs/word (reads)
and incremental flash from 1568 ms to 246 ms. Each step was landed as code, but
the *principles* transfer — to the Xtensa port, to other probes, to any
host-driven debug transport. This doc is the checklist: **when porting or
optimizing, walk these patterns in order.** Each entry: the principle, where it
lives in our code, the measured impact, and what to check when applying it
elsewhere.

Ordering matters: the patterns are sorted by leverage. Do not tune pattern 6
while pattern 1 is unverified — a single configured throttle dominates
everything downstream (we tuned around ours for hours).

---

## 1. Verify the link is at its rated speed before optimizing anything

**Principle.** Find every configurable clock/divider in the path and prove each
is at its intended value. A leftover conservative setting silently scales ALL
other numbers.

**Our case.** `__init__` set TCK divider 20 (1.2 MHz on a 24 MHz base) with a
comment from the reset-only era. Every benchmark for two days was 20× throttled;
a second bug (an unimported constant eaten by `except Exception`) then pinned
the "fix" at div=2. Reads went 77→36 µs/word from the divider alone.

**Porting check.** Read the device's capability descriptor (don't assume);
assert the divider you set is the one in effect; never wrap the speed setup in
a silent exception handler. *(transport.py `__init__`, caps descriptor 0x2000.)*

## 2. One USB round-trip per logical operation

**Principle.** Count the bus round-trips in one logical op (a memory read, a
register save). Setup, payload, status-check, and teardown can almost always
ride one queue. Round-trips are ~250 µs each on USB FS; everything else is
noise until these are gone.

**Our case.** `read_mem` paid 4 round-trips (SBA setup / burst / SBCS check /
SBCS clear) — folded into ONE `_dmi_batch` (600→384 µs single-word).
`call_function` did ~30 individual register accesses — now one batched read
stream + one write stream (12.6→6.3 ms per ROM call, before the stub removed
the calls entirely).

**Porting check.** Xtensa NAR access is the same shape: queue NAR writes/reads
behind one state setup instead of per-register exchanges.

## 3. Writes need no capture — stream them, check sticky status once

**Principle.** If the protocol latches errors stickily, a write stream needs
zero read-back until a single status check at the end. No capture → no IN
traffic → no IN-FIFO limit → the OUT stream free-runs.

**Our case.** `_dmi_stream_writes`: OUT-only DMI writes + ONE DTMCS `dmistat`
read; on a sticky error the WHOLE stream is redone via the slow path
(correctness preserved, fast path pays nothing). This is how OpenOCD writes at
27 µs/word; ours: 75→33.

**Porting check.** Find the sticky-status equivalent (Xtensa XDM: DOSR error
bits). The fallback path must exist and be exercised by tests.

## 4. Capture only the bits you will actually read

**Principle.** Response fields are usually narrower than the scan. Per-bit
capture control means the IN budget (FIFO, bandwidth) buys more operations.

**Our case.** DMI responses carry 34 useful bits of a 44-bit scan;
capture-narrowing fits ~25% more scans per IN-FIFO chunk. *(`_scan(capture=(lo,
hi))`, `_scan_dr_resp`.)*

## 5. Drain IN concurrently — the device's flow control is the scheduler

**Principle.** If the device NAKs OUT when its IN buffers fill, then a
concurrent IN reader converts the FIFO *limit* into a FIFO *depth*: the limit
bounds in-flight data, not throughput. Stop ping-ponging chunks; stream both
directions and let NAK pacing do the rest.

**Our case.** `_dmi_batch_async`: reader thread drains IN while OUT streams.
Reads went 30→20.5 µs/word (C6) and killed the "OpenOCD is faster on C5"
mystery (42→23.3 vs their 27). Write side: a builder→writer queue hides encode
time under transfer. No nogil reliance: blocking libusb calls release the GIL;
correctness comes from Queue/join.

**Porting check.** Confirm the NAK-on-full behavior in the protocol doc before
trusting it; bound the reader by *data progress* (consecutive-empty counter),
never by wall-clock alone.

## 6. Compress the command stream where the protocol allows

**Principle.** Command streams are repetitive even when payloads aren't. A
repeat/RLE primitive shrinks reads (constant shift-in) far more than writes.

**Our case.** REP nibbles (`0xC+digit`, base-4 LSB-first, probe-rs is the
reference encoding). Wire-neutral for random write data, but read-burst OUT
streams collapse ~5× — a key ingredient in pattern 5's read numbers. CAUTION:
RLE does NOT save device time — each repeat still costs a TCK; we falsified
"RLE will fix the write wall" by measurement, which is what exposed pattern 1.

## 7. Memoize deterministic command streams

**Principle.** If an operation's byte stream is a pure function of its
arguments (make it so by opening with a state-reset), cache the packed bytes
and skip the entire host-side encode.

**Our case.** `dmi_read` / single-word `read_mem` templates open with a
TAP-reset walk → state-independent → cached per address. Single-word read:
590→247 µs (probe-rs parity). Invalidate when stream-shaping state changes
(the DTM idle hint).

## 8. Move the inner loop on-chip (resident stub + mailbox)

**Principle.** When per-call overhead (halt/resume/register shuffling)
dominates, stop calling — install a tiny resident loop on the target and feed
it commands through memory, which pattern 2/3 made cheap.

**Our case.** 228-byte position-independent stub: CRC of 64 sectors = ONE
command (was 128 ROM calls); erase+program pipelined. Incremental flash 370→246
ms; the no-change rescan is 97 ms. Per-chip differences ride the mailbox (ROM
fn pointers), so one blob serves every chip.

**Porting check.** The stub needs: a scratch carve-out, a clean return-to-debug
(our ebreak trap), and target-independent staging (pattern 9). Mind callee
stack demands — ROM tinfl wanted 11 KB of stack and silently destroyed the
carve-out (#36's finding).

## 9. Overlap host transfer with device work

**Principle.** Anything the host can do while the target is busy is free. The
question per protocol: which host channel is independent of the running core?

**Our case.** RISC-V SBA works while the hart runs → stage sector n+1 during
erase/program of sector n (`call_rom_begin`/`_call_finish`, the stub's double
buffers). Staging (~35 ms/sector) hides entirely under erase (24–62 ms).

**Porting check.** Xtensa has no background SBA — memory access needs the core
halted (instruction injection), so this pattern needs the stub (pattern 8) to
do the overlap target-side instead.

## 10. Choose work units by measured hardware cost

**Principle.** Device-side units (erase sizes, command granularities) have
non-linear costs. Measure per-die, per-unit; don't assume datasheet typicals.

**Our case.** 64 KiB block erase = 4.4–5.7× faster than 16×4 KiB sector erases
on every vendor; flash dies vary 2.6× between "identical" boards
(FLASH-DIE-SURVEY.md). Guarded by the never-erase-what-you-won't-rewrite
invariant (the pyOCD `fast_program` bug class).

---

## The discipline patterns that made the speed safe

- **Byte-accounting tripwire** (`drain_mode="validate"`): an assert-empty check
  that catches any IN-stream desync loudly. Every transport change today was
  landed under it; it caught the ZLP bug within minutes.
- **Never trust a value read after a failed precondition.** Stale DATA0 after
  abstract-command errors masqueraded as flash corruption for an hour (#33);
  `read_register` now raises instead of returning garbage. Same lesson at the
  scratch-buffer level (stale staging read as "flash content").
- **Hardware-free invariant tests with realistic semantics.** The NOR-AND fake
  (writes can only clear bits) makes a wrong skip-erase decision corrupt
  simulated content — bugs fail tests instead of bricking boards.
- **Record every run keyed by git sha** (`flash_bench --record`, jtag-tests.db):
  per-fix progression is a graph, not a memory. The fastest way to catch a
  regression is a labelled point that moved the wrong way.
- **Verify transcriptions against the upstream source of truth.** The C5 WDT
  table was wrong because it was hand-derived while OpenOCD's cfg had it
  verbatim; `verify_chips_vs_tcl.py` now executes the upstream Tcl against a
  recording backend and diffs our tables in every check run.
- **One gate after every change** (`check.py`, `--mock` no-hardware / `--real`
  on-target): invariant + drift + S3 flasher model + both chips' selftests +
  3-way cross-tool dump + recorded bench. If it's green, the
  change shipped with evidence.
