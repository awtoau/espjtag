# espjtag vs OpenOCD — JTAG/DMI correctness & timing audit

> **Status of the findings (don't trust prose for status — check the tracker).**
> Of the two correctness items this audit found:
> - `dmi_write` now checks op-status and retries on busy (raising if it never
>   succeeds), and `dmi_read` now **raises** on retry-exhaustion instead of
>   returning the busy read's data — these are in the code (transport.py
>   `dmi_write`/`dmi_read`; see the `did not succeed` raises). Tracked under the
>   never-close timing/correctness audit, [espjtag#12](https://github.com/awtoau/espjtag/issues/12).
> - the **DTMCS DMIRESET on sticky busy** is **NOT** in the code. An earlier change
>   that added it was reverted; on a busy op espjtag today only bumps `self.idle`
>   and retries, with no `dtmcs.dmireset`. Tracked as
>   [espjtag#25](https://github.com/awtoau/espjtag/issues/25) (open). Treat area 6 /
>   TL;DR item 1's DMIRESET recommendation as outstanding.
>
> The **IN-endpoint drain** is removed in the code: default `drain_mode="off"`
> (area 1), consistent with the benchmark rebuild
> ([JTAG-BENCHMARK-ANALYSIS.md](JTAG-BENCHMARK-ANALYSIS.md),
> [ESPJTAG-STORY.md](ESPJTAG-STORY.md)); the audit tracker is again
> [espjtag#12](https://github.com/awtoau/espjtag/issues/12). Other items remain
> open: the conservative `FIFO_CHUNK_BITS=480` vs the 1024-bit device limit
> ([espjtag#23](https://github.com/awtoau/espjtag/issues/23)), the missing
> `wdt_disable` on halt ([espjtag#24](https://github.com/awtoau/espjtag/issues/24)),
> and the timing nits (TCK divisor, per-op TAP reset / IR re-select, idle floor —
> [espjtag#8](https://github.com/awtoau/espjtag/issues/8)). Read the bodies as
> "what the audit found"; for current state, follow the issue links.

Audit of the pure-Python `espjtag` client against OpenOCD's reference
`esp_usb_jtag` + RISC-V 0.13 driver, looking for every place espjtag does
something OpenOCD doesn't — ranked **correctness first, timing second**.

- espjtag (ours): `/home/dan/git/espjtag/espjtag/{transport,debug,constants,reset,timing}.py`
- OpenOCD ref (pinned `f10eceff22fb8dcd3db69bf3ebc5c70602454af6`):
  - `src/jtag/drivers/esp_usb_jtag.c` (local copy `tmp/esp_usb_jtag.c`)
  - `src/jtag/drivers/bitq.c` (the scan model)
  - `src/target/riscv/{riscv-013.c, riscv.c, batch.c}` (the DMI logic — the
    Espressif fork has refactored the classic monolithic `dmi_scan`/`dmi_op`
    into a **batch** model with an **adaptive learned delay**; see notes)
  - `tcl/target/esp32c6.cfg`, `tcl/interface/esp_usb_jtag.cfg`

Bench evidence: xiao-c6-b (`1-1.3.1.3.4`, IDCODE `0x0000dc25`, abits=7, dtmcs.idle=1)
and xiao-c5 (`1-1.2`, IDCODE `0x00017c25`, abits=10, dtmcs.idle=7), both read-only,
left running. Raw logs: `espjtag/tmp/audit_probe.log`, `audit_probe2.log`
(scripts `espjtag/scripts/audit_probe.py`, `audit_probe2.py`).

> **Architecture note that reframes several areas.** The pinned Espressif
> OpenOCD does NOT keep the TAP parked and re-shift only DR. Its bitq layer
> ends *every* DR/IR scan in `TAP_IDLE` (`batch.c` `jtag_add_dr_scan(..., TAP_IDLE)`)
> and `bitq_state_move` walks the **shortest TMS path** between stable states —
> it never issues a 5×TMS=1 TLR reset except on an explicit `JTAG_TLR_RESET`
> command. The RISC-V layer re-selects IR=DMI implicitly per scan-command but
> the JTAG core caches IR and only re-shifts it when the selected IR actually
> changes. So OpenOCD's "normal" per-op cost is: *(optional 0–2 TMS to re-enter
> Shift-DR from IDLE) + DR scan + idle*. No TLR, no IR re-shift, no IN drain.

---

## TL;DR — correctness risks first

*(Status differs per item — see the banner and the issue links. Bodies kept as the
record of what was found and why.)*

1. **[CORRECTNESS — OPEN, [espjtag#25](https://github.com/awtoau/espjtag/issues/25)]
   DMI busy-retry never issued `dtmcs` DMIRESET** (transport.py
   `dmi_read`). OpenOCD's `increase_dmi_busy_delay` does
   `dtmcs_scan(DTM_DTMCS_DMIRESET)` *before every busy retry* (riscv-013.c). After
   a sticky/busy DMI, the DTM can latch an error that only DMIRESET clears; without
   it espjtag's retries can all see the same busy. (The "returns busy data on
   exhaustion" half of this is fixed — `dmi_read` now raises; but the DMIRESET on
   busy was reverted and is **not** in the code, so this finding stays open.)
2. **[CORRECTNESS — done in code, [espjtag#12](https://github.com/awtoau/espjtag/issues/12)]
   `dmi_write` originally ignored op-status entirely** (was transport.py L395-396).
   `def dmi_write(...): self._dmi(address, data, DMI_WRITE)` — `_dmi` returns
   `(data, op)` but `dmi_write` drops it, so a write that returns op==3 (busy) or
   op==2 (failed) is silently lost. OpenOCD retries writes on busy exactly like
   reads. A dropped DMCONTROL/COMMAND/SBADDRESS write during a reset or burst is a
   real wedge/garbage risk.

Everything else is timing or benign. The dominant *timing* loss is **not** any
of the JTAG niceties OpenOCD does — it's two USB-latency things:

- **[TIMING, dominant] the residual IN-endpoint drain.** The "fixed" drain is
  still the #1 cost. `drain_mode="validate"` (the current default) measured
  **~1875 µs/op on C6, ~1723 µs/op on C5**, vs **~333 µs/op with `drain_mode="off"`**
  — a **5.5× per-op penalty** that buys only a periodic assertion.
  `drain_mode="always"` is ~3500–3715 µs/op. (audit_probe2.log)
- **[TIMING] TCK 20× slower than OpenOCD.** Caps descriptor (read live on both
  parts) is `base_speed_khz=24000, div_min=1, div_max=255`. espjtag hardcodes
  SETDIV=20 → **1.2 MHz**; OpenOCD's `esp_usb_jtag.cfg` asks `adapter speed 40000`,
  clamped to div_min=1 → **24 MHz**. In-range (no correctness risk) but 20× slow.

---

## The full table

| # | Area | espjtag does | OpenOCD does | Divergence | Class | Est. cost | Fix |
|---|------|--------------|--------------|------------|-------|-----------|-----|
| 1 | IN-endpoint handling | `_recv` reads exactly `ceil(bits/8)` rounded **up to wMaxPacketSize**; `drain_mode="validate"` still does a ~3 ms empty-read every N ops | `pending_in_bits` accounting; `recv_buf` reads exactly `(pending+7)/8` capped at `IN_BUF_SZ`; **never drains**; mid-stream recv only when `pending > (IN_BUF_SZ+hw_in_fifo_len-1)*8` | validate-drain residual + read rounded to MPS not exact | **TIMING** | ~1.5 ms/op (validate vs off) | default `drain_mode="off"`; keep "validate" opt-in |
| 2 | TCK divisor / SETDIV | hardcoded `SETDIV=20` → 1.2 MHz; never reads caps | reads caps (base/div_min/div_max), `adapter speed 40000`→div 1→24 MHz | 20× slower TCK; ignores caps | **TIMING** (benign for correctness — 20 ∈ [1,255] confirmed) | TCK ~42 µs/op vs ~2 µs; tiny next to USB, but free win | read caps, pick div in `[div_min,div_max]`; default div=1–2 |
| 3 | TAP reset frequency | `reset_tap()` (5×TMS=1 + goto IDLE) **per DMI op** | never TLRs except explicit reset; ends scans in IDLE, walks shortest TMS path | per-op TLR = 18 extra TCK (C6) | **TIMING** (benign correctness) | ~18 TCK/op ≈ 15 µs @1.2 MHz | drop per-op `reset_tap`; reset once at session start |
| 4 | IR re-selection | `_scan_ir(IR_DMI)` **every op** (33 TCK on C6) | caches IR; re-shifts only on change | extra IR scan per op | **TIMING** | ~33 TCK/op ≈ 27 µs @1.2 MHz | select IR=DMI once, hold across ops (the batch path already does) |
| 5 | Idle cycles | `_idle(max(self.idle,1))` after every scan; **mutates `self.idle` on busy, never resets** | adaptive learned delay seeded from `dtmcs.idle`, only `runtest(delay)` when `delay>0`; reset at examine | forces ≥1 even if dtmcs.idle==0; permanent idle inflation after one busy | **TIMING** (mostly) / minor correctness | small | use `self.idle` (0 allowed); reset learned idle per session |
| 6 | DMI busy/retry | bump `self.idle`, retry; **no DMIRESET**; **returns busy data on exhaustion**; `dmi_write` drops status | DMIRESET each retry, increase delay, loop until `command_timeout`; distinguishes BUSY/FAILED | missing DMIRESET; write status ignored | **CORRECTNESS** | — | add DMIRESET on busy; retry writes; raise on exhaustion |
| 7 | 2-phase DMI read | `dmi_read` = READ-scan then NOP-scan in one IR session; batch `read_mem` slices `[2:2+n]` | batch handles read pipeline (read returns previous access) | espjtag's "+2 slot" shift correct but coupled to fixed scan order | **BENIGN** (verified on bench) | — | add an assert that the read-phase slot is discarded |
| 8 | scan width abits+34 | `width = abits+34`; abits from dtmcs `(v>>4)&0x3F`, idle `(v>>12)&0x7` | `abits + DTM_DMI_DATA_LENGTH(32) + DTM_DMI_OP_LENGTH(2)` | **identical** | **BENIGN** | — | none (matches) |
| 9 | System Bus Access | `read_mem`: sbreadonaddr+readondata+autoincrement, `n+1` reads + NOP, slice `[2:2+n]`; checks SBCS sberror/sbbusyerror **once after burst**, W1C, fall back to slow path | bus_v1 sets same SBCS bits; checks sberror/sbbusyerror once after batch, W1C, abort/retry | espjtag adds a guard+fallback OpenOCD lacks; `write_mem32` SBCS=0x40000 vs c6.cfg's 0x48000 (readondata set) — see area 10 | **BENIGN** (read path is *safer* than OpenOCD) | — | none; optionally clear readondata before final word |
| 10 | reset_run_from_rom | ports esp32c6_soc_reset SBA writes + riscv deassert + halt/dcsr(0x90c3)/resume | esp32c6.cfg `riscv dmi_write` sequence + riscv-013 deassert | SBCS value & a missing `wdt_disable`; `sleep` values differ — see below | **mostly BENIGN**, 1 ordering nit | — | match SBCS=0x48000, consider wdt_disable |
| 11 | misc | `time.sleep(0.05)` reset settle; `self.idle` never reset; `dmi_write` no readback | `jtag_sleep` 10 ms; learned-delay reset at examine | magic 50 ms vs 10 ms; idle inflation | **TIMING/BENIGN** | 40 ms/reset | use 10 ms like the cfg |

---

## Area-by-area detail

### 1. IN-endpoint handling — the documented exemplar (drain) + two more

**The exemplar (already fixed, kept as the model finding).**
OpenOCD never drains. It tracks `priv->pending_in_bits` precisely
(`esp_usb_jtag.c:215`, incremented in `esp_usb_jtag_out` at `:526` only when
`tdo_req`), and `esp_usb_jtag_recv_buf` (`:354`) reads **exactly**
`ct = (pending_in_bits+7)/8` (capped at `IN_BUF_SZ=64`), explicitly noting at
`:366-369` that the adapter's IN endpoint *does not emit 0-byte packets*, so a
read when nothing is pending is wrong and is skipped. Mid-stream it only recvs
when the queue is genuinely deep: `while (pending_in_bits > (IN_BUF_SZ +
hw_in_fifo_len - 1)*8) recv_buf()` (`:449`, `hw_in_fifo_len=4` at `:727`).

espjtag's old code drained the IN endpoint before every op. Because an *empty*
`ep_in.read` does **not** honour a 1 ms timeout (libusb/kernel floors it at
~3 ms on this Full-Speed device), every op paid ~3 ms. The fix replaced it with
`drain_mode` (transport.py L99-172). **But the bench shows the fix is
incomplete:** the current default `drain_mode="validate"` with `_validate_every`
starting at 1 still fires the ~3 ms empty-read on the early ops of every short
session (a reset is <20 ops, so backoff barely kicks in).

> Measured (audit_probe2.log), 20× `dmi_read(dmstatus)`:
> | mode | C6 µs/op | C5 µs/op |
> |---|---|---|
> | off | **332.9** | **335.1** |
> | validate (default) | 1875.5 | 1723.1 |
> | always | 3715.5 | 3427.9 |

So **the default is ~5.5× slower than `off` for no correctness benefit on a
session that's already proven correct.** OpenOCD's answer is simpler: precise
accounting + *never read when nothing is pending*. espjtag's `_recv` already
reads exactly the captured byte count, so the endpoint is empty afterward — the
validate drain is asserting an invariant the precise read already guarantees.
**Recommendation: default `drain_mode="off"`.** Keep "validate" as an opt-in CI
knob, not the production default.

**Two further IN-read divergences:**
- **`_recv` rounds the read length UP to wMaxPacketSize** (`rdlen = ceil(need/mps)*mps`,
  transport.py L195) to dodge a libusb *Overflow* on a short read. OpenOCD reads
  the **exact** `ct` bytes (`recv_buf` `:362`). Benign (the extra bytes are the
  device's own zero-pad from CMD_FLUSH), but it means espjtag can ask for up to
  63 bytes more than pending. Harmless because the device only sends what it
  flushed; noted for completeness.
- **No IN-FIFO chunk threshold in the single-op path.** OpenOCD's
  `(IN_BUF_SZ + hw_in_fifo_len - 1)*8 = 536` bit threshold is mirrored only in
  `_dmi_batch` (`FIFO_CHUNK_BITS=480`, transport.py L448). The single-op
  `dmi_read`/`_dmi` never approach it (one scan ≈ 41–138 bits), so fine — but if
  anyone raises the per-op scan count they must respect 536. Benign today.

### 2. TCK divisor / SETDIV — **20× slower, but in-range**

transport.py L94: `self.dev.ctrl_transfer(0x40, 0, 20, 0, None)` — SETDIV=20,
unconditional, caps never read. **Bench-confirmed** caps on *both* parts:
`base_speed_khz=24000, div_min=1, div_max=255` (raw desc `010a0108c0120100ff00`:
proto_ver=1, len=10, speed type=1 len=8, apb=0x12c0=4800 → base=4800·10/2=24000,
div_min=1, div_max=255).

- div=20 → TCK = 24000/20 = **1.2 MHz**, comfortably inside [1,255] → **not a
  correctness risk on C6 or C5** (assumption that 20 might be out of range:
  *disproven*).
- OpenOCD's `interface/esp_usb_jtag.cfg` requests `adapter speed 40000`, clamped
  by `esp_usb_jtag_khz` to `div_min=1` → **24 MHz** actual. So OpenOCD drives
  these parts **20× faster** than espjtag.

Cost is modest because USB latency dominates (a 1.2 MHz op is ~333 µs of which
TCK shifting at ~342 bits/op is only ~285 µs of *wire* but most of that is the
two USB round-trips, not bit-banging). At 24 MHz the shift time is negligible;
the op stays USB-bound at ~150 µs/transfer. Still a free ~1.4× on the burst path.
**Recommendation: read the caps descriptor (code already in `audit_probe.py`),
pick `divisor = clamp(base/desired_khz, div_min, div_max)`; default to div_min
or 2.** No correctness downside measured.

### 3. TAP reset frequency — per-op TLR is pure waste

transport.py: `_dmi` (L386), `dmi_read` (L408), `read_dtmcs` (L367) each call
`reset_tap()` = 5×TMS=1 then BFS to IDLE. Measured **18 TCK/op** attributed to
`reset_tap` on C6 (audit_probe2.log).

OpenOCD's `bitq_state_move` (verbatim) walks `tap_get_tms_path()` — the
*shortest* path between stable states — and **never** issues a TLR except on an
explicit `JTAG_TLR_RESET` command. Every scan ends in `TAP_IDLE`
(`batch.c`: `jtag_add_dr_scan(..., TAP_IDLE)`), so the next op starts from a known
IDLE with **0–2 TMS** to re-enter Shift, no reset.

espjtag *already tracks* `self.state` correctly via `_NEXT`/`_goto`, so the
per-op `reset_tap` is redundant defensive code, not a correctness need. It
survives in `_dmi`/`dmi_read` even after the batch path proved the TAP can stay
synced. **Recommendation: reset once at session open (or on detected
desync), then rely on the tracked state + `_goto`** — exactly what `_dmi_batch`
already does (one `reset_tap` for the whole batch, L460). ~18 TCK/op ≈ 15 µs at
1.2 MHz; small vs USB, but it also adds OUT nibbles that bloat the packet.

### 4. IR re-selection — extra IR scan per op

transport.py `_dmi`/`dmi_read` call `_scan_ir(IR_DMI)` every op = **33 TCK/op**
on C6 (5-bit IR + select/capture/update TMS overhead, ×1 TAP; ~16 on C5 oddly
*less* because of how the path lands). OpenOCD's JTAG core caches the loaded IR
and `bitq`/scan only re-shifts IR when the requested IR differs, so consecutive
DMI ops re-use IR=DMI with no IR scan. `_dmi_batch` already does this (one
`_scan_ir` per batch, L461). **Recommendation: hold IR=DMI across consecutive
single ops too**; couple with area 3 (reset+IR-select once per session).

### 5. Idle cycles — count is right, but two nits

- espjtag clocks `_idle(max(self.idle,1))` after the DMI DR scan (transport.py
  L390, L411, L414). dtmcs.idle read correctly: C6=1, C5=7 (bench). OpenOCD
  seeds its learned delay from `dtmcs.idle` at examine
  (`reset_learned_delays` → `riscv_scan_set_delay(..., dtmcs_idle)`) and only
  emits `jtag_add_runtest(delay, TAP_IDLE)` **when `delay>0`** (batch.c
  `get_delay`/`add_idle_before_batch`). So if a part reported `dtmcs.idle==0`,
  OpenOCD clocks **zero** idle; espjtag forces **1**. Neither C6 nor C5 hits this
  (idle≥1), so benign here — but the `max(...,1)` is an unjustified floor.
- **espjtag inflates `self.idle` permanently on busy and never resets it**
  (`dmi_read` L426 `self.idle += 1`; also in `read_mem`'s implied retries). After
  one transient busy, *every subsequent op forever* clocks an extra idle. OpenOCD
  grows its delay the same way **but resets to `dtmcs.idle` at each examine**
  (`reset_learned_delays`). Minor timing leak; reset `self.idle` per session.

### 6. DMI busy/retry + error handling — **the real correctness bugs**

espjtag `dmi_read` (transport.py L398-427):
```python
for _ in range(retries):            # retries=8
    ... issue READ + NOP, read back ...
    if status == 0: return data, status
    if status == 3: self.idle += 1
return data, status                 # <-- on exhaustion, returns BUSY data!
```
OpenOCD (`increase_dmi_busy_delay`, riscv-013.c): on busy it (a) issues
`dtmcs_scan(target->tap, DTM_DTMCS_DMIRESET, NULL)` to clear the sticky DTM
error, then (b) `riscv_scan_increase_delay(...)`, and the caller
(`batch_run_timeout`) loops `while time < command_timeout`. Two espjtag gaps:

1. **No DMIRESET.** A DMI access that left the DTM `op` field sticky-busy stays
   busy until DMIRESET; espjtag's retries can spin uselessly and then **return
   the busy value as data** — a *fast wrong answer*. CORRECTNESS.
2. **`dmi_write` discards op-status** (L395-396: `_dmi(...)` returns a tuple,
   `dmi_write` ignores it). A write that comes back busy/failed is silently
   dropped — no retry, no error. In a reset sequence a dropped DMCONTROL is a
   wedge; in an SBA setup a dropped SBADDRESS0 is wrong-address reads.
   CORRECTNESS.

**Recommendation:** on `status==3`: do a DTMCS DMIRESET (IR=DTMCS, write
`dmireset` bit) then retry; on exhaustion **raise**, don't return. Apply the same
retry to `dmi_write`. Distinguish `status==2` (FAILED → raise immediately) from
`status==3` (BUSY → retry).

### 7. 2-phase DMI read — robust, verified

espjtag `dmi_read` issues READ@addr then NOP in one IR=DMI session and takes the
NOP-phase capture (transport.py L405-422); `read_mem` issues the address write +
`nwords+1` SBDATA0 reads + a trailing NOP and slices `[2:2+nwords]`
(debug.py L148-155). This correctly composes the **two** pipelines (DTM
read-returns-previous + SBA readondata). OpenOCD's batch hides the DTM pipeline
inside `riscv_batch_get_dmi_read_data`. Bench: `read_mem(256)` returns the same
first word as a known flash image (`0x11018082` on C6) and matched the per-word
path in prior testing. **BENIGN.** Only nit: the "+2 slot" offset is a magic
constant tied to the exact `[addr_write, read, read…, nop]` order — add an assert
that `res[0]` is the address write and `res[1]` is discarded, so a future reorder
fails loudly rather than silently shifting the window.

### 8. scan width / dtmcs parsing — exact match

`width = self.abits + 34` (transport.py L384) == OpenOCD `abits +
DTM_DMI_DATA_LENGTH(32) + DTM_DMI_OP_LENGTH(2)`. `abits=(v>>4)&0x3F`,
`idle=(v>>12)&0x7` match `DTM_DTMCS_ABITS_OFFSET=4`, `DTM_DTMCS_IDLE_OFFSET=12`
(upstream.lock). Bench: abits=7 (C6)/10 (C5) as expected. **BENIGN.** Fragility
note: `_ensure_dtmcs` only reads dtmcs if `abits` is falsy; if a first DMI op ran
before `read_dtmcs` on a hypothetical abits==0 part it would re-read — fine in
practice, but the width depends on a value read by a *different* method, so keep
`read_dtmcs` mandatory in `examine`.

### 9. System Bus Access — espjtag is actually *safer* than OpenOCD

`read_mem` (debug.py L126-167) sets SBCS = sbreadonaddr|sbreadondata|
sbautoincrement|sbaccess=2 (`_sb_setup`), pipelines `nwords+1` SBDATA0 reads,
then **reads SBCS and checks `SB_SBERROR|SB_SBBUSYERROR`**, W1C-clears, and
**falls back to a proven per-word path** on any error or any DTM op==3 in the
burst (L156-165). OpenOCD's bus_v1 checks the same SBCS bits once after the batch
and W1C-clears, but **aborts** (`ERROR_FAIL`) rather than transparently retrying
slow. espjtag's guard+fallback is *stronger*. SBCS field offsets all match
upstream.lock (sbaccess=17, autoinc=16, readondata=15, readonaddr=20).
**BENIGN / better.** One OpenOCD nicety espjtag could copy: clearing
`sbreadondata` before the final read so the last word doesn't arm one bus access
past the requested range (espjtag instead over-reads by one word into a NOP slot
— harmless for memory-mapped flash, but could fault on a region whose next word
is unmapped/MMIO). Worth a guard for non-flash reads.

### 10. reset_run_from_rom / soc_reset — cross-check vs esp32c6.cfg

esp32c6.cfg `esp32c6_soc_reset` (verbatim order from the pinned cfg):
```
dmi_write DMCONTROL 0x80000001     # haltreq|dmactive
dmi_write SBCS      0x48000        # sbaccess=2 (0x40000) | sbreadondata (0x8000)
dmi_write SBADDR0   0x600b1034
dmi_write SBDATA0   0x80000000     # HPSYS_SW_RESET
dmi_write DMCONTROL 0             # clear dmactive (drop sbbusy)
dmi_write SBCS      0x48000
dmi_write SBADDR0   0x600b1038
dmi_write SBDATA0   0x10000000     # CPU_CORE0_SW_RESET
dmi_write DMCONTROL 0
dmi_write DMCONTROL 0x40000001     # resumereq|dmactive
sleep 10                           # ms
poll
esp32c6_wdt_disable
... resume / reset-assert ...
```
espjtag `_c6_soc_reset` (debug.py L249-266) matches the **register addresses,
values, and the clear-dmactive-between-SBA-writes** exactly, and uses
`time.sleep(0.01)` = 10 ms = the cfg's `sleep 10` (correctly commented).
Divergences:
- **SBCS value.** espjtag's `write_mem32` → `_sb_setup(size_access=2)` writes
  **SBCS=0x40000** (no `sbreadondata`); the cfg writes **0x48000** (readondata
  set). For a pure *write* the readondata bit is harmless (no read is triggered),
  so benign — but it's a literal divergence from the captured sequence. If you
  want byte-for-byte fidelity, set bit15 here.
- **`esp32c6_wdt_disable` is omitted.** The cfg disables the RTC/Super WDT after
  the SW reset. espjtag doesn't. For the "boot the app" use-case this is benign
  (the app reconfigures its own WDT), but if a reset lands the core in a window
  where the WDT fires before the app starts, OpenOCD is more robust. Low risk;
  note it.
- **deassert ordering is correct.** OpenOCD's riscv013 `deassert_reset` polls
  `while (ALLUNAVAIL && !ALLHAVERESET)` then writes ackhavereset. espjtag's
  `_deassert_reset` (debug.py L268-277) breaks on `not(ALLUNAVAIL) or
  ALLHAVERESET` = the exact De Morgan negation. ✓ And it sets haltreq during the
  ndmreset assert (0x80000003), matching the cfg (which ends soc_reset with
  0x80000003) — correct for the C6 even though a *generic* `reset run`
  (reset_halt=false) would not set haltreq. The halt→dcsr(0x90c3)→resume
  handshake (`_halt_go`/`_set_dcsr_ebreak`/`_resume_go`) mirrors what OpenOCD does
  on a spontaneously-reset hart. **BENIGN with the two nits above.**

### 11. Misc — magic times, assumed values, sign/endian

- **`reset_run()` `time.sleep(0.05)`** (debug.py L199, reset.py L44) — 50 ms
  ndmreset settle, vs the cfg's 10 ms. 5× longer than OpenOCD; not justified in a
  comment beyond "let reset settle". Recommend 10 ms to match, or measure.
- **`self.idle` never reset** (area 5) — the only persisted assumed-value leak.
- **Endianness:** `_bits_to_int` is LSB-first (constants.py L25), capture order
  is LSB-first per byte (matches the protocol doc "lowest bit in every byte
  captured first", esp_usb_jtag.c:38-40). DMI word assembly `(address<<34)|
  (data<<2)|op` matches `DTM_DMI_*_OFFSET`. SBA addresses masked to 32 bits.
  No sign issues (all unsigned masks). ✓
- **OpenOCD waits we could *beat*:** OpenOCD's `esp_usb_jtag_sleep` flushes then
  `jtag_sleep(us)` busy-waits the host (it even TODO-notes it could send dummy
  clocks instead). espjtag's idle is on-wire TCK, which is *better* for short
  settles. And OpenOCD reads the caps descriptor with a fixed 256-byte control
  transfer twice (TODO at riscv `:658`) — minor. Neither is worth chasing.

---

## Prioritized fix list

*(For status, follow the issue links — don't trust a checkmark in prose. The
timing/correctness items are surfaced in the
[espjtag#12](https://github.com/awtoau/espjtag/issues/12) audit tracker; the
TCK-divisor and per-op-reset_tap levers are also in
[#8](https://github.com/awtoau/espjtag/issues/8), WDT-disable on halt is
[#24](https://github.com/awtoau/espjtag/issues/24), and the DMIRESET-on-busy item is
[#25](https://github.com/awtoau/espjtag/issues/25). This list stays here as the
area-by-area record of why each one matters.)*

**Correctness (do first):**
1. **Split — part done, part open.** `dmi_read`/`dmi_write` busy handling: the
   **raise on retry-exhaustion** (no longer returning busy data) and the
   **op-status check + retry on `dmi_write`** are in the code
   ([#12](https://github.com/awtoau/espjtag/issues/12)). But the **DTMCS DMIRESET on
   op==3** is **NOT** in the code (an earlier add was reverted) and distinguishing
   op==2 (raise immediately) from op==3 (retry) is still outstanding — tracked as
   [#25](https://github.com/awtoau/espjtag/issues/25). (area 6)
2. Add an assert in `read_mem` that the discarded read-pipeline slots are where
   expected, so a reorder can't silently shift the data window. (area 7) — *still open
   (cheap guard).*

**Timing (high value, low risk):**
3. **Done in code** — default `drain_mode="off"` (drain removed; "validate" opt-in).
   The single biggest win ([#12](https://github.com/awtoau/espjtag/issues/12)). (area 1)
4. Read the caps descriptor and set divisor from `[div_min,div_max]` (default
   div_min/2); 20× faster TCK, free. (area 2)
5. Stop per-op `reset_tap` + `_scan_ir`: reset + select IR=DMI **once per
   session**, rely on tracked state (as `_dmi_batch` already does). (areas 3,4)
6. Use `self.idle` without the `max(...,1)` floor; reset learned idle per
   session. (area 5)
7. `reset_run` settle 50 ms → 10 ms to match the cfg. (area 11)

**Fidelity nits (optional):**
8. `write_mem32` in the reset path: SBCS 0x40000 → 0x48000 to match esp32c6.cfg
   byte-for-byte; consider `esp32c6_wdt_disable`. (area 10)
9. Clear `sbreadondata` before the final word of a burst for non-flash regions.
   (area 9)

## Bench appendix

`scripts/audit_probe.py <path>...` — caps descriptor decode, IDCODE/DTMCS,
20× dmi_read + 256-word read_mem timing. `scripts/audit_probe2.py` — per-op TCK
breakdown by phase + drain-mode A/B/C. Both read-only.

Measured (pinned run, 2026-06-10):
- caps (C6 & C5): base 24000 kHz, div 1..255 → div=20 in-range, 20× slow vs OpenOCD's 24 MHz.
- dtmcs.idle: C6=1, C5=7 (read correctly).
- per-op TCK: C6 342 total = reset_tap 18 + ir_select 33 + dr_scan 276 + idle 12 → 15% reset/IR overhead.
- drain: off 333 µs/op · validate 1875/1723 µs/op · always 3716/3428 µs/op (C6/C5).
