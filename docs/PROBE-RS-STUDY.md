# probe-rs as a second reference for `espjtag`

A source study of [probe-rs](https://github.com/probe-rs/probe-rs) — the modern,
pure-Rust embedded debug toolkit — used as an **independent second implementation**
to cross-check our pure-Python `espjtag` (`~/git/espjtag`). probe-rs speaks the
*same* `esp-usb-jtag` transport we do, plus RISC-V *and* Xtensa *and* ARM, so it is
the ideal reference for protocol correctness, performance, the Xtensa port (espjtag
#9), flash-over-JTAG (#3), and per-chip data (#4).

**Scope:** pure source reading. No hardware, no probe-rs build, no espjtag changes.

**Pinned commit:** `8a7d9ebf9241ac80fca826181c97580f5211ad04` (2026-06-08). All paths
below are under that tree; permalinks are
`https://github.com/probe-rs/probe-rs/blob/8a7d9eb/<path>`.

**Repo layout that matters to us:**
- `probe-rs-espressif/` — the ESP-specific crate: the `esp-usb-jtag` probe driver,
  per-chip reset/flash sequences, and the chip-data YAMLs.
- `probe-rs/src/probe/common.rs` — the *generic* JTAG TAP layer (state machine,
  scan-chain interrogation, `JtagAccess`), shared by every JTAG probe.
- `probe-rs/src/architecture/{riscv,xtensa}/` — arch layers built on top of
  `JtagAccess`, transport-agnostic.
- `probe-rs/src/flashing/` — the generic RAM-flash-loader machinery (CMSIS-pack
  style), used unchanged for ESP.

espjtag files referenced: `espjtag/transport.py`, `espjtag/debug.py`,
`espjtag/reset.py`, `espjtag/constants.py`.

---

## TL;DR — STEAL THESE (ranked by value)

| # | Borrow from probe-rs | Why | espjtag issue | Effort |
|--:|----------------------|-----|---------------|:------:|
| 1 | **CMD_REP / RLE on the OUT stream** — collapse runs of identical CLK nibbles into the protocol's repeat command (`0xC+n`, base-4 counter, max 1023 reps/cmd) | We emit one nibble per TCK; idle runs, BYPASS pads, the 5-clock TAP reset, and long DR scans of constant TDI compress hugely. probe-rs does this in `push_command`/`add_repetitions`. Shrinks OUT payload → fewer 64-B packets per batch. | #6, #8 | S–M |
| 2 | **Deferred-result queue with auto-skip-capture** — schedule ops, flush lazily; a result handle is an `Arc<()>` and if the caller dropped it, the batch *doesn't capture* its TDO bits at all | This is a cleaner, more general version of our `_dmi_batch`. The auto-skip means writes never waste IN-FIFO budget. Generalises batching past `read_mem` to *any* op sequence (register reads, reset handshakes). | #1, #8 | M |
| 3 | **progbuf + `abstractauto` memory burst** (RISC-V) | A *second*, often faster path than SBA for RAM/flash: program buffer runs `lw s1,(s0); addi s0,s0,4`, `abstractauto` makes every `DATA0` read auto-fire it, so N words = N single-scan `DATA0` reads. Crucial where SBA is slow/absent (probe-rs routes C6 external-flash `0x4200_0000` region to progbuf, not SBA). | #3, #8 | M–L |
| 4 | **The Xtensa model** (`xtensa/xdm.rs` ≈1100 lines + `communication_interface.rs`) — NAR/NDR instruction pair, register/memory via instruction injection into DIR0EXEC + DDR, `Lddr32P` auto-increment burst | This is the *entire blueprint* for espjtag #9 (S2/S3 over the same transport). Clean, portable, and the memory-read pipeline mirrors the RISC-V one we already understand. | #9 | L |
| 5 | **Data-driven per-chip YAMLs** (`probe-rs-espressif/targets/*.yaml`) — memory map, core type(s)+TAP index, flash-algo blob, the lot | Exactly the per-chip table model we want for #4. We can *reference* (not vendor) these: memory maps, the C5/P4 "CPU on TAP 1" fact, reset-register addresses. | #4 | S (reference) / M (adopt format) |
| 6 | **ROM-magic chip disambiguation** — read 32 bits at `0x4000_1000`, match against a per-IDCODE `variants` map | IDCODE alone doesn't separate C6 variants (all `0xdc25`). probe-rs reads a ROM magic word to pick the exact target. Our `_CHIP_SIG` is IDCODE-only and would mis-ID revisions. | #4 | S |
| 7 | **Generic scan-chain interrogation** (`extract_idcodes` + `extract_ir_lengths`, ported from Glasgow) | Replaces our hardcoded `_CHAIN_BY_IDCODE` C5 special-case with a general algorithm: walk the IDCODE DR, infer IR lengths from `10`-start patterns. Handles any multi-TAP chain. | #4 | M |
| 8 | **RAM flash-loader (CMSIS-pack) mechanism** — upload an algo blob to RAM, set GPRs+PC+return-breakpoint, run, wait for halt; call exported `Init/ProgramPage/EraseSector` + vendor `FlashSize/ChipRevision` | The clean architecture for #3. ESP uses the *same* `Flasher` as ARM; only the blob and the IDF image layout (via `espflash`) are ESP-specific. | #3 | L |

**Cross-check verdict:** probe-rs **confirms** our two headline findings —
(a) **no per-op drain** (it tracks `pending_in_bits` precisely and never drains
before an op), and (b) the **C6 reset recipe** (identical SBA writes to
`0x600b1034`/`0x600b1038` with a `Dmcontrol(0)` between them to clear `sbbusy`).
It **diverges** on the post-flash-ROM boot problem (it does *not* do our
USB-bus-reset trick) — see §3, which is the one place this study suggests we might
be papering over something.

---

## 1. The `esp-usb-jtag` transport

**probe-rs:** `probe-rs-espressif/src/espusbjtag/protocol.rs` (the wire protocol)
and `.../espusbjtag/mod.rs` (the `DebugProbe`/`RawJtagIo` glue).
**espjtag:** `espjtag/transport.py`.

The two are remarkably close — same VID/PID (`0x303A:0x1001`), same vendor-class
`0xFF` interface discovery, same 4-bit command nibbles. probe-rs additionally
handles the ESP-USB-**Bridge** firmware (PID `0x1002`, caps in a string descriptor)
which we don't need.

### Nibble framing
Identical encoding. probe-rs `Command::from`
(`protocol.rs:548`): `Clock = (cap<<2)|(tms<<1)|tdi`, `Flush = 0xA`,
`Reset = 8|srst`, `Repeat = 0xC+n`. Our `constants._clk` is the same `(cap<<2)|(tms<<1)|tdi`
and `CMD_FLUSH=0xA`. We **don't implement** `Reset` (0x8/0x9, the SRST line — ESP
USJ has no usable SRST anyway) or `Repeat` (0xC–0xF) — see §1 RLE below.

### IN endpoint: probe-rs CONFIRMS "no drain"
This is the single most important cross-check. probe-rs **does not drain before
every op.** It keeps a precise `pending_in_bits` counter
(`protocol.rs:68,295-309,484-490,494-531`) and only reads IN bits it knows it
queued. The only "draining" is:
- a **one-time startup flush** (`new_from_selector`, `protocol.rs:250-286`) to
  recover from a wedged device, and
- **back-pressure relief mid-stream**: after each OUT it reads IN *only if*
  `pending_in_bits > (IN_EP_BUFFER_SIZE + HW_FIFO_SIZE) * 8` (`protocol.rs:486`),
  i.e. `(64+4)*8 = 544` bits — it drops down to "one bufferful pending", never to
  zero.

This is exactly our model: `_recv` reads precisely `want_tdo_bits`, leaving the
endpoint empty, so the pre-op drain we removed was defending against a
byte-accounting bug that precise accounting already prevents. **probe-rs proves
the architecture is right.** Our `drain_mode="validate"` (assert-empty, backing
off) is a *stricter* safety net than probe-rs has — keep it; it's cheap insurance,
not a correctness crutch.

### The IN-FIFO chunk limit
- **probe-rs**: hard cap of **128 bytes = 1024 captured bits** per command stream,
  cited from the **ESP32-S3 TRM** verbatim (`protocol.rs:296-309`): *"[A] command
  stream can cause at most 128 bytes of capture data to be generated [...] without
  the host acting to receive the generated data."* It flushes *before* it would
  capture the 1025th bit.
- **espjtag**: we chunk at `FIFO_CHUNK_BITS = 480`, derived as
  `< (IN_BUF_SZ + hw_in_fifo_len - 1)*8 = 536` (`transport.py:448`).

**Finding:** our 480/536 is **conservative by ~2×** vs probe-rs's TRM-sourced 1024.
The two thresholds describe *different* limits, though, and both matter:
- 1024 bits = the **device-side capture buffer** before the *command stream stalls*
  (the OUT side blocks). This is the one in the TRM.
- ~544 bits = the **host-side back-pressure point** probe-rs uses to decide when to
  *drain mid-stream* so the OUT EP doesn't block.

We conflated these into one number. **Action:** raise our per-flush chunk toward
~1000 bits (matching the real capture-buffer limit), and add probe-rs's separate
"relieve IN back-pressure after each OUT once >~544 pending" rule. This is a direct
perf win (fewer flush round-trips per batch) for #8.

### TCK divisor / capabilities descriptor
- **probe-rs** *parses* the caps descriptor (`protocol.rs:177-231`): reads
  `base_speed_khz`, `div_min`, `div_max` from the speed-APB capability
  (type `1`). BUT — `EspUsbJtag::set_speed` (`mod.rs:104-109`) is a **stub** that
  returns the requested value and **never issues a SETDIV control transfer**.
  `speed_khz()` reports `base_speed/div_min` for display only.
- **espjtag** hardcodes `SETDIV=20` via one control transfer
  (`transport.py:94`: `ctrl_transfer(0x40, 0, 20, 0, None)`).

**Finding:** neither tool actually *tunes* TCK at runtime, and probe-rs doesn't even
program a divisor (it runs at the device default). This **confirms our own
conclusion** (the espjtag USB cost model; perf tracker
[espjtag#8](https://github.com/awtoau/espjtag/issues/8)) that **TCK speed is irrelevant** at
Full-Speed-USB round-trip scale — the bottleneck is the ~1 ms frame, not the bit
clock. We could drop the hardcoded `SETDIV=20` entirely (probe-rs proves you don't
need it), or keep it for determinism. **Don't bother reading div_min/div_max to
"pick a speed"** — probe-rs reads them and then ignores them.

### RLE (the one thing we're missing in the transport)
probe-rs collapses **consecutive identical** `Clock` commands using the protocol's
repeat mechanism (`push_command` `protocol.rs:326-341`, `add_repetitions`
`409-424`). The repeat counter is base-4 (`repeat_count × 4^cmd_rep_count`), max
**1023** reps per command run. We emit one nibble per clock unconditionally.

Wins for us: the 5-clock TAP reset, runs of idle (RTI) cycles, BYPASS padding on
the C5, and especially **long constant-TDI stretches** inside DR scans (a DMI scan
shifts mostly-zero data → long runs of `CLK(tms=0,tdi=0,cap=1)`). For a 41-bit C6
DMI scan, most bits are an identical capturing-clock with tdi=0; RLE could shrink
the OUT bytes for a 12-scan batch substantially. This is our speed-ideas **#6**,
and probe-rs is the proof it works in practice. **Effort:** small — it's a tweak to
`_emit`/`_send` to coalesce identical nibbles into `0xC+n` runs.

---

## 2. RISC-V debug

**probe-rs:** `architecture/riscv/dtm/jtag_dtm.rs` (the DTM = our DMI layer),
`architecture/riscv/communication_interface.rs` (DM logic, ~3265 lines).
**espjtag:** `transport.py` (DMI) + `debug.py` (DM).

### DMI access — essentially identical to ours
`JtagDtm::dmi_register_access` (`jtag_dtm.rs:118-177`) and `DmiOperation`
(`691-747`) match our `_dmi`/`dmi_read`/`dmi_write` bit-for-bit:
- scan = `(address<<34) | (data<<2) | op`, width `abits+34` (`jtag_dtm.rs:798-802`,
  our `transport.py:388`).
- **2-phase read**: `schedule_read` issues `Read{address}` then a `NoOp` to collect
  (`jtag_dtm.rs:322-328`) — exactly our "scan READ@addr then NOP" comment
  (`transport.py:399-402`).
- **busy/retry**: op-status `3 = RequestInProgress` → `clear_error_state`
  (write `dtmcs.dmireset`) + **`set_idle_cycles(idle+1)`** + retry
  (`jtag_dtm.rs:161-166`, `267-277`). We now do the same (`transport.py:425-426`:
  `self.idle += 1` **plus** a `dtmcs.dmireset` on sticky busy — the audit's
  correctness fix, since landed; previously we only bumped idle). `dmi_write` also
  retries on busy now. (`DTMCS_ADDRESS=0x10`, bit 16.)
- `dtmcs` decode identical: `abits[9:4]`, `idle[14:12]`, `version[3:0]`,
  require version==1.

### Where probe-rs is structurally better: the deferred queue
Every DMI op is **scheduled** into a `Queue` (`jtag_dtm.rs:131-147`,
`probe/queue.rs`) and flushed lazily by `execute()`. The result of each op is a
`DeferredResultIndex` you redeem later. Two things make this elegant:
1. **Auto-skip-capture** (`queue.rs:252-257`): the index is an `Arc<()>`; if the
   caller dropped their handle, `should_capture()` is false and `write_register_batch`
   (`common.rs:739-806`) **doesn't capture those TDO bits**. Writes therefore cost
   *zero* IN budget automatically — no manual "is this a write?" bookkeeping like
   our batch does.
2. **Partial-retry on busy** (`jtag_dtm.rs:251-311`): `execute` consumes the
   commands that succeeded, bumps idle, and re-runs only the remainder.

Our `_dmi_batch` (`transport.py:450-492`) achieves the same *throughput* for the
specific `read_mem` case (one IR-select, chunked OUT/IN), but it's a single-purpose
function. probe-rs's queue is **general**: register reads, the reset handshake,
memory bursts — all become "schedule N, flush once." **This is the model to adopt
for #1/#8** if/when we generalise batching beyond `read_mem`.

### Abstract commands & `abstractauto`
- **GPR/CSR**: `abstract_cmd_register_read/write` via `AccessRegisterCommand`
  (aarsize, transfer, write, postexec, regno) — identical fields to our
  `read_register`/`write_register` (`debug.py:76-89`, `constants.py:78-87`).
- **`abstractauto`** (`communication_interface.rs:758-772`): at examine time it
  *probes* whether the target supports autoexec by writing all-ones to
  `abstractauto` and reading back. We don't use autoexec at all.

### Memory — THREE methods, picked per region (we have one)
probe-rs has a `MemoryAccessMethod` enum and a per-region override map
(`communication_interface.rs:220-280`):

1. **System Bus Access** (`perform_memory_read_multiple_sysbus`,
   `communication_interface.rs:1530-1585`) — **the method we use**. Identical
   recipe: `sbcs` with `sbreadonaddr + sbreadondata + sbautoincrement`, write
   `Sbaddress0`, then a pipeline of `Sbdata0` reads (our `read_mem`,
   `debug.py:126-167`). probe-rs writes `Sbcs(0)` before the *last* read to stop
   the auto-trigger; we read `nwords+1` then NOP. Same idea, same `sberror`/
   `sbbusyerror` re-check + retry.

2. **Program buffer + autoexec** (`read_multiple_autoexec`,
   `communication_interface.rs:1708-1802`) — **we don't have this.** Setup: progbuf
   = `lw s1,0(s0); addi s0,s0,4`; prime so `DATA0=M[addr]`, `s1=M[addr+4]`; enable
   `abstractauto.autoexecdata`; then **each `DATA0` read returns one word and
   auto-fires the command** to advance. N words ≈ N single-scan `DATA0` reads
   (chunked 256 at a time, `:1729`). Mirrors OpenOCD's
   `read_memory_progbuf_inner`. **Used only when `supports_autoexec && len>=16`**
   (`:1883`) — below that it falls back to per-word postexec.

3. **Abstract command** — single-word fallback.

**Why this matters for us (#3, #8):** the C6 sequence routes external-flash space
`0x4200_0000..0x4300_0000` to **WaitingProgramBuffer, not SBA**
(`esp32c6.rs:55-80`) — *"Loading external memory is slower than the CPU. If we
can't access something via the system bus, select the waiting program buffer
method."* So **SBA does not reach memory-mapped flash on the C6**; reading flash
contents over JTAG needs the progbuf path. Our SBA-only debugger can read RAM/MMIO
fine but **cannot read the XIP flash window** — worth knowing before we build
flash verify/read over JTAG.

**Verdict:** our SBA burst is correct and as fast as probe-rs's SBA burst for RAM.
For flash-over-JTAG and for chips/regions where SBA is unavailable, we need the
progbuf+autoexec path. Port `read_multiple_autoexec` — it's ~90 lines of clear
logic and we already understand the DMI/abstract-command primitives it needs.

---

## 3. RISC-V reset — probe-rs CONFIRMS our recipe, but boots differently

**probe-rs:** `probe-rs-espressif/src/sequences/esp32c6.rs`
(`reset_system_and_halt`, `:95-135`).
**espjtag:** `debug.py:_c6_soc_reset` (`:249-266`), `reset_run_from_rom`
(`:279-358`), `reset.py:reset_run`.

### The SBA soc-reset writes are IDENTICAL
probe-rs `reset_system_and_halt`:
```
halt
write_dm_register Sbcs(0x48000)
write_dm_register Sbaddress0(0x600b1034)
write_dm_register Sbdata0(0x80000000)   // LP_AON HPSYS_SW_RESET
write_dm_register Dmcontrol(0)          // clear dmactive -> clears sbbusy
write_dm_register Sbcs(0x48000)
write_dm_register Sbaddress0(0x600b1038)
write_dm_register Sbdata0(0x10000000)   // LP_AON CPU_CORE0_SW_RESET
write_dm_register Dmcontrol(0)          // clear sbbusy again
Dmcontrol{dmactive, resumereq}
sleep 10ms
Dmcontrol{dmactive, ackhavereset}
enter_debug_mode; on_connect; reset_hart_and_halt
```
Our `_c6_soc_reset` does the **same** two SBA writes to the **same** addresses with
the **same** values and the **same** `Dmcontrol(0)`-between-to-clear-sbbusy, ending
with the resume/ack handshake. Both cite "ported from OpenOCD" — independent
ports landing on the identical sequence is strong confirmation our reset is right.

(`Sbcs(0x48000)` = `sbaccess=2` (32-bit, bits[19:17]) `| sbreadonaddr` ... — their
`_sb_setup` equivalent for a write.)

### Watchdog disable — we're MISSING this
`ESP32C6::disable_wdts` (`esp32c6.rs:29-53`) is run `on_connect` and `on_halt`: it
unlocks and zeroes the **super-WDT, TG0/TG1 WDTs, and RTC WDT** (write-protect
key `0x50D83AA1`). If you halt a C6 and *don't* feed/disable the WDTs, the RTC/SWD
watchdog can reset the chip out from under you during a debug session. Our
`espjtag` never touches the WDTs. **Action (#3, #9):** add a `disable_wdts(C6)`
helper and call it after halt — addresses, keys, and bit (`RTC_CNTL_SWD_AUTO_FEED_EN`,
bit 18 of `0x600B_1C1C`) are all in `esp32c6.rs:29-53`. This likely explains any
flakiness when we hold the C6 halted for more than a moment.

### The post-flash-ROM boot: where we DIVERGE (and may be over-engineering)
Our hard-won `reset_run_from_rom` does a **USB bus reset (`USBDEVFS_RESET`)** to
"clear the BOOT-strap-LOW download latch," then a JTAG reset handshake — proven 3/3
on xiao-c6-b. **probe-rs does NO USB bus reset.** Its `reset_system_and_halt` relies
purely on the SBA soc-reset + `reset_hart_and_halt` + `enter_debug_mode`.

Two readings, kept separate:
- **probe-rs's use case is different.** probe-rs *attaches over JTAG and stays
  attached*; it flashes via its **own RAM flash-loader** (§5), so the chip is never
  in esptool's USB-Serial download stub when it resets. Our problem is specifically
  *recovering from `esptool ... --after no-reset`*, i.e. the chip is parked in the
  **ROM USB-serial downloader** with the strap still latched. probe-rs simply never
  enters that state, so it never needs to clear it. This is the most likely
  explanation and means **both can be right**.
- **OR** the SBA soc-reset (HPSYS + CPU_CORE0 software reset) is a *fuller* reset
  than the bare `ndmreset` we measured as "0/3," and **probe-rs's sequence alone
  might boot from ROM without the USB reset** — in which case our USB-reset trick is
  a workaround for using too-weak a reset, not for a strap latch. We measured
  `ndmreset`-only and OpenOCD `reset run` as 0/3 from the download state, but did we
  measure the **full `_c6_soc_reset` (the LP_AON SW-reset writes) WITHOUT the USB
  reset** from that same state? If not, that's the experiment to run.

**Action (#3):** test `_c6_soc_reset` + deassert handshake **alone** (no
`USBDEVFS_RESET`) from a fresh post-flash `--after no-reset` state. If it boots
3/3, our USB-reset step is unnecessary and we've been blaming a strap latch for what
was really an insufficient reset. If it still fails, the USB-reset trick is genuine
and probe-rs avoids the problem only by not entering download mode — document that
firmly. Either way this resolves the one open question in our reset story.

### `reset_hart_and_halt` / `enter_debug_mode`
probe-rs factors the generic "halt the hart and confirm" and "bring up the DM"
into `RiscvCommunicationInterface` methods reused by every sequence. Our `examine`,
`_halt_go`, `_deassert_reset`, `_resume_go`, `_set_dcsr_ebreak` cover the same
ground (and we ported the `dcsr <- 0x90c3` ebreak step OpenOCD does on a
spontaneous reset, which probe-rs's generic path handles internally). We're at
parity on the handshake; probe-rs is just better factored.

---

## 4. Xtensa debug — the blueprint for espjtag #9

**probe-rs:** `architecture/xtensa/xdm.rs` (~1100 lines, the low-level XDM),
`architecture/xtensa/communication_interface.rs` (~1500 lines, halt/regs/memory),
`architecture/xtensa/arch/instruction/` (the instruction encoder),
`probe-rs-espressif/src/sequences/esp32s3.rs`.
**espjtag:** none yet — this is issue #9.

This is the highest-value *new* capability we can port, and probe-rs hands us a
clean, transport-agnostic model. The S2/S3 use the **same `esp-usb-jtag` transport**
we already have (`espusbjtag/mod.rs:158-167` wires `try_get_xtensa_interface` to the
very same `EspUsbJtag`), so **our entire transport layer is reusable as-is** — only
the debug-module layer is new.

### How it works (the whole protocol, concisely)
1. **TAP access is a NAR/NDR pair** (`xdm.rs:30-59`, `416-451`). IR=`0x1C` selects
   the Nexus address register. You scan an **8-bit NAR** = `(regaddr<<1)|rw`, whose
   capture returns a **2-bit status** (0=ok, 1=error, 2=busy), then a **32-bit
   NDR** for the data. This is the Xtensa analog of our RISC-V DMI scan — and it
   fits our `_scan_ir`/`_scan_dr` primitives directly (different IR, different
   widths, same machinery).
2. **Power-up** (`enter_debug_mode`, `xdm.rs:185-257`): TAP reset; write
   `PowerControl` (IR=`0x08`) to assert debug-reset + wakeups; set
   `jtag_debug_use`; poll `DebugStatus` bit 31 (`dbgmod_power_on`) until set; clear
   `core_was_reset`/`debug_was_reset`; verify `OCDID != 0/0xFFFFFFFF`; enable OCD in
   `DebugControl`. ESP has **no RISC-V Debug Module** here — it's a wholly different
   register set (PWRCTL/PWRSTAT/DSR/DCR/DDR/DIR), all listed with bitfields in
   `xdm.rs:768-1102`.
3. **Halt** (`xdm.rs:543-568`): set `DebugControl.{enable_ocd, debug_interrupt}`;
   clear pending break/int status bits.
4. **Registers via instruction injection** (`communication_interface.rs:536-614`):
   there's no "read register" command. To read CPU reg `Ax`: execute
   `WSR Ax -> DDR` (write the reg to the Debug Data Register), then read DDR. To
   read a **special** reg: `RSR special -> Ax(scratch)` first, via a scratch GPR
   (A3), tracked by a **register cache** that saves/restores clobbered regs.
   Instructions are written to **DIR0EXEC** (write-and-execute) and completion is
   polled via `DebugStatus.exec_done` (`xdm.rs:627-693`, `746-766`).
5. **Memory via auto-increment load** (`communication_interface.rs:675-761` +
   `Lddr32P`): load the base address into A3, then repeatedly execute
   `LDDR32.P A3` — "load 32 from (A3), A3 += 4, result -> DDR" — and read DDR each
   time. **This is structurally identical to the RISC-V SBA/autoexec burst**: one
   address setup, then a pipeline of single-scan DDR reads. Handles unaligned
   head/tail by masking a word. Writes use `SDDR32.P`.
6. **Resume** (`xdm.rs:606-625`): clear pending bits, execute `RFDO(0)`.

### How much code, and is it portable to Python?
- **XDM core**: ~400 lines of logic + ~340 lines of bitfield register definitions
  (`xdm.rs`). The bitfields port to a Python constants module trivially (they're
  just bit offsets, like our `constants.py`).
- **Instruction encoder**: small — only a handful of instructions are needed
  (`RSR`, `WSR`, `LDDR32.P`, `SDDR32.P`, `RFDO`). The encodings are in
  `xtensa/arch/instruction/`. We'd hand-transcribe ~6 opcodes.
- **comm interface**: the halt/read/write/memory logic is ~600 lines but much is
  the register-cache and unaligned-handling polish; a *minimal* "halt + read GPR +
  read memory" port is a few hundred lines.

**Verdict:** **yes, this is a clean model to port.** The shape is the same as the
RISC-V debugger we already have — `(transport scan) -> (debug-module register
access) -> (halt/regs/memory)` — just with NAR/NDR instead of DMI and
instruction-injection instead of SBA/abstract-commands. Start with: power-up +
`OCDID` read (proves the transport reaches the Xtensa TAP), then halt, then a single
GPR read, then the `LDDR32.P` memory burst. The S3's two cores sit on **separate
TAPs** (`esp32s3.yaml:11-20`, `jtag_tap: 0/1`) — our existing `taps_before/after`
BYPASS machinery handles selecting between them.

One safety note probe-rs flags but hasn't implemented:
`xdm.rs:179` *"TODO implement openocd's esp32_queue_tdi_idle() to prevent
potentially damaging flash ICs."* Worth carrying that caveat forward.

---

## 5. Flash algorithms — RAM stub, not direct SPI

**probe-rs:** `probe-rs/src/flashing/flasher.rs` (the generic loader),
`probe-rs/src/flashing/flash_algorithm.rs`, `probe-rs-espressif/src/image_format.rs`
(the IDF image builder), the `flash_algorithms:` block in each target YAML.
**espjtag:** we have no flash-over-JTAG yet (#3); today we flash via `esptool`.

### The mechanism (this is the answer to "RAM stub or direct SPI?")
**RAM stub.** probe-rs does **not** bit-bang SPI. It uses the standard CMSIS-pack
flash-loader pattern, *identical for ARM and ESP*:
1. `reset_and_halt` the core (`flasher.rs:200`).
2. **Download the algorithm blob to RAM** at `algo.load_address`
   (`flasher.rs:207-209`) — for the C6 that's `0x40810000`
   (`esp32c6.yaml:60`), a ~3 KB position-independent RISC-V blob stored
   base64 in the YAML.
3. **Call exported functions by setting GPRs + PC + a return breakpoint, then
   running** (`call_function`/`call_function_and_wait`, `flasher.rs:940-1000`):
   `Init(addr, clk, fn)` → `EraseSector(addr)` / `ProgramPage(addr, size, buf)` →
   `UnInit`. Entry PCs are named in the YAML (`pc_init`, `pc_program_page`,
   `pc_erase_sector`, `pc_erase_all`, `pc_verify`, `pc_read`).
4. **ESP-specific vendor functions** (`image_format.rs:91-110`,
   `flasher.rs:901-938`): `FlashSize` (PC `0x708`) and `ChipRevision` (PC `0x700`)
   are called the same way to autodetect the part before flashing.
5. Page data is staged in a RAM buffer; `transfer_encoding: miniz`
   (`esp32c6.yaml:90`) means probe-rs **compresses page data with miniz/deflate**
   before writing it to the algo's buffer, and the blob decompresses on-target —
   fewer bytes over the slow link.

The blob *internally* drives the SPI flash controller (it's Espressif's loader
code, related to esptool's flash stubs), but from probe-rs's side it's just
"upload code, call functions." The **IDF image layout** (bootloader +
partition-table + app at the right offsets) is built by the `espflash` crate
(`image_format.rs:149-169`), then handed to the generic `FlashLoader` as raw
`(addr, bytes)` segments.

### What this means for espjtag #3
- A full flash-over-JTAG for us = (a) obtain/port a RAM flash-loader blob, (b)
  implement "upload blob + call function with GPRs/PC + return-breakpoint + wait
  for halt" on top of our existing memory-write and halt/resume. The
  function-call mechanism is small and generic; it's the same primitive whether
  the blob is ARM or RISC-V.
- We could **reuse probe-rs's exact C6/C5/H2/P4 blobs and PC offsets** (they're in
  the YAMLs, MIT/Apache-licensed) rather than building our own — reference them for
  #4 and #3 together.
- **But** for our actual need (boot a freshly-flashed app), the simpler path
  remains: let `esptool` flash, then `reset_run_from_rom` to boot. Flash-over-JTAG
  is the *bigger* project; the RAM-stub approach is the clean way to do it if we
  commit, and avoids re-implementing SPI controller poking.

---

## 6. Chip data files — the data-driven model for #4

**probe-rs:** `probe-rs-espressif/targets/*.yaml` (e.g. `esp32c6.yaml` = 90 lines,
`esp32s3.yaml` = 153 lines), schema in `probe-rs-target/src/`.
**espjtag:** `debug.py:_CHIP_SIG` + `transport.py:_CHAIN_BY_IDCODE` — two small
hardcoded dicts keyed by IDCODE.

A target YAML is exactly the per-chip table we want for #4. What's in one (C6):
- **`chip_detection`**: `idcode: 0xdc25` + a `variants` map keyed by the **ROM magic
  word** (see below).
- **`cores`**: name + `type` (`riscv`/`xtensa`) + `core_access_options` (for Xtensa,
  the `jtag_tap` index — this is where the S3's cpu0/cpu1 TAP assignment lives).
- **`memory_map`**: typed regions (`!Nvm`/`!Ram`/`!Generic`) with `range`,
  per-core visibility, and access flags. C6: flash `0x0..0x1000000` (boot),
  ROM `0x40000000..0x40050000`, RAM `0x40800000..0x40880000`, XIP-flash alias
  `0x42000000..0x43000000`.
- **`jtag.scan_chain`**: `ir_len: 5` per TAP — feeds the generic scan-chain code.
- **`flash_algorithms`**: the blob + all the PC offsets + `flash_properties`
  (page size `0x4000`, sector `0x10000`, timeouts) + `transfer_encoding: miniz`.
- **`default_binary_format: idf`** and `rtt_location`.

### ROM-magic chip disambiguation (we'd get this wrong)
`probe-rs-espressif/src/lib.rs:48-97`: because several variants share an IDCODE
(all C6 = `0xdc25`), probe-rs reads a **32-bit magic word from ROM at
`0x4000_1000`** and matches it against the YAML `variants` map to pick the exact
target (e.g. `0x2ce0806f -> esp32c6`). For chips where the CPU is on TAP 1 (C5/P4)
it short-circuits on a `variants[0]` entry because it can't read memory before
selecting the right TAP (`lib.rs:80-83`).

Our `_CHIP_SIG`/`_CHAIN_BY_IDCODE` are **IDCODE-only**, so they'd mis-identify
revisions and can't distinguish variants that share an IDCODE. **Action (#4):** add
the ROM-magic read (`read_mem32(0x40001000)`) to our chip ID, and structure our
per-chip data as `{idcode: {magic: chipname}}` mirroring the YAML.

### Should we reuse the YAMLs directly?
- **For data we can't easily derive** (memory maps, flash-algo blobs + PC offsets,
  the C5/P4 TAP-1 fact, reset-register addresses), **reference them** — they're a
  curated, tested source under a permissive license. Worth a tracked note in our
  per-chip table citing the YAML.
- **For our small needs** (IDCODE → abits, chain layout, reset register addrs), a
  thin Python dict mirroring the relevant YAML fields is enough; we don't need the
  full schema. Adopt the *shape* (`idcode`/`magic`/`memory_map`/`reset regs`), not
  the whole format.

---

## 7. Architecture & patterns worth borrowing

probe-rs's layering, bottom to top:
```
DebugProbe / RawJtagIo          (espusbjtag: shift_bit + read_captured_bits)
  -> JtagAccess                 (common.rs: TAP state machine, scan chain, register r/w)
     -> DTM / XDM               (riscv DMI / xtensa NAR-NDR; the deferred Queue lives here)
        -> CommunicationInterface (halt, regs, memory, per-region method selection)
           -> Core / Session    (multi-core, MemoryInterface, DebugSequence hooks)
```
espjtag is flatter: `EspUsbJtagTransport` (transport + TAP + DMI) → `EspUsbJtag`
(DM: halt/regs/mem/reset). That's fine for one transport + one arch, but two probe-rs
seams are worth copying as we grow:

1. **`RawJtagIo` ↔ `JtagAccess` split.** The probe only implements `shift_bit` +
   `read_captured_bits` + a `JtagState`; *everything else* (TAP state walking,
   scan-chain interrogation, IR/DR register r/w with BYPASS padding) is the generic
   `AutoImplementJtagAccess` blanket impl (`common.rs:570-807`). For us, the payoff
   is **the Xtensa port shares 100% of the transport** — adding S2/S3 means a new
   *debug-module* layer, zero transport changes. Our `_scan_ir`/`_scan_dr`/`_goto`
   are already the right primitives; just keep the arch-specific DMI/NAR-NDR logic
   in a layer *above* them (debug.py already mostly does this).

2. **`DebugSequence` hooks** (`on_connect`, `on_halt`, `reset_system_and_halt`,
   `on_unknown_semihosting_command`) — per-chip behaviour as overridable methods
   rather than `if chip == "c6"` branches. Our reset code already half-does this
   (`_c6_soc_reset`); formalising a small per-chip sequence object would scale to
   C5/H2/P4/S3 without a tangle of conditionals.

**Error handling:** probe-rs uses `thiserror` enums per layer (`RiscvError`,
`XtensaError`, `DmiOperationError`, XDM `Error`) with `docsplay::Display`. Our
`RuntimeError` strings are coarser; typed exceptions for `DmiBusy`/`SbaError`/
`AbstractCmdError` would make retry logic cleaner (probe-rs's per-error retry in
`execute` is only possible because the errors are typed). Low priority, but the
*deferred-queue + typed-error retry* combo (§2) is the genuinely elegant bit.

**Multi-core / Permissions:** the YAML lists cores and a region's per-core
visibility; `Session` mediates `Permissions` (e.g. erase-all gating). Not relevant
to our single-core-at-a-time use, but the S3's two-Xtensa-cores-on-two-TAPs model
is something our BYPASS machinery already supports — note it for #9.

---

## 8. Performance numbers / claims

**probe-rs publishes no USJ throughput numbers** in-repo (no benchmark vs OpenOCD
in `doc/` or the changelog that this study found). The performance posture is
encoded in the *design*, not a headline figure:
- **Batch everything** via the deferred queue; flush at the IN-FIFO/back-pressure
  limits (1024-bit capture cap; ~544-bit mid-stream drain trigger).
- **RLE** the OUT stream so big batches fit fewer 64-byte packets.
- **Compress flash data** (`miniz`) to cut bytes over the slow link.
- **autoexec/auto-increment bursts** so N-word memory reads are N single-scan reads
  behind one setup, not N full round-trips.
- It does **not** chase TCK speed (the `set_speed` no-op), confirming the bottleneck
  is USB round-trips, not bit rate — **exactly our cost model**
  (~1 ms Full-Speed frame floor; perf tracker
  [espjtag#8](https://github.com/awtoau/espjtag/issues/8)).

So probe-rs offers no number to beat, but it **validates our entire speed strategy**
(batch + keep-IR + SBA-autoinc + RLE + don't-tune-TCK) as the right set of levers.
The one place it's ahead of our *current* code is RLE (§1) and the generality of its
batching (§2); the one place its design implies a *bigger* chunk than we use is the
1024-bit capture limit (§1).

---

## Appendix: cross-check scorecard

| Our finding | probe-rs says | Verdict |
|---|---|---|
| No per-op drain needed (precise `_recv` leaves IN empty) | Same — tracks `pending_in_bits`, never pre-drains, only relieves back-pressure >544 bits | **CONFIRMED** |
| TCK speed irrelevant; hardcode SETDIV=20 | Reads div_min/max but `set_speed` is a **no-op**, never programs a divisor | **CONFIRMED** (we could even drop SETDIV) |
| 2-phase DMI read (READ then NOP), busy=3 → idle+1 retry | Identical, plus writes `dtmcs.dmireset` on busy | **CONFIRMED**; dmireset on sticky busy **now added** (was the audit's correctness fix) |
| C6 reset = SBA writes to 0x600b1034/0x600b1038 + clear sbbusy + ndmreset/resume | **Byte-identical** sequence | **CONFIRMED** (independent OpenOCD port) |
| FIFO chunk at 480 bits (<536) | TRM capture limit is **1024 bits**; 544 is a separate back-pressure point | **We're ~2× conservative** — raise it (perf win) |
| `read_mem` via SBA auto-increment burst | Same recipe; but **SBA can't reach C6 XIP flash** — needs progbuf there | **CONFIRMED for RAM**; gap for flash |
| `reset_run_from_rom` needs a USB bus reset to clear strap latch | probe-rs does **no** USB reset (never enters esptool download mode) | **DIVERGENT** — run the "soc_reset alone, no USB reset" experiment to settle whether our USB-reset step is necessary |
| (we don't disable WDTs) | probe-rs disables super/TG0/TG1/RTC WDTs on connect+halt | **GAP** — add WDT disable for stable halts |
| (we don't do autoexec/progbuf) | progbuf+abstractauto burst, used for len≥16 and where SBA is absent | **GAP** — port for flash reads / #8 |
| (no Xtensa) | Full XDM model over the same transport, ~1100 lines | **BLUEPRINT for #9** |
