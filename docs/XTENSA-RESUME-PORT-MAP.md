# Port map — Xtensa resume/register-cache (the #29 "verbatim, full" port)

The dependency closure of `xtensa_resume` / `xtensa_run_algorithm`, mapped from
openocd-esp32 `src/target/xtensa/xtensa.c` **@ `f10eceff22fb8dcd3db69bf3ebc5c70602454af6`**
before porting any of it (per `FAITHFUL-REIMPLEMENTATION.md`: read all → map deps →
port leaves first).

Arrows = "calls / depends on". **Port in reverse-topological order: leaves
(bottom, no outgoing arrows) first, each tested before anything above it.**

```mermaid
graph TD
  run_algorithm[xtensa_run_algorithm] --> start_algorithm[xtensa_start_algorithm]
  run_algorithm --> wait_algorithm[xtensa_wait_algorithm]

  start_algorithm --> reg_get[xtensa_reg_get]
  start_algorithm --> reg_set[xtensa_reg_set]
  start_algorithm --> write_sr_by_num[xtensa_write_sr_by_num]
  start_algorithm --> resume[xtensa_resume]

  wait_algorithm --> reg_get
  wait_algorithm --> write_dirty[xtensa_write_dirty_registers]

  resume --> prepare_resume[xtensa_prepare_resume]
  resume --> do_resume[xtensa_do_resume]

  prepare_resume --> reg_set
  prepare_resume --> cause_get[xtensa_cause_get]
  prepare_resume --> do_step[xtensa_do_step]
  prepare_resume --> write_dirty

  do_resume --> cause_reset[xtensa_cause_reset]
  do_resume --> queue_exec_ins[xtensa_queue_exec_ins]
  do_resume --> dm_queue_execute[xtensa_dm_queue_execute]
  do_resume --> core_status_check[xtensa_core_status_check]

  do_step --> cause_get
  do_step --> cause_clear[xtensa_cause_clear]
  do_step --> reg_get
  do_step --> reg_set
  do_step --> prepare_resume
  do_step --> do_resume
  do_step --> fetch_all_regs[xtensa_fetch_all_regs]
  do_step --> pc_in_winexc[xtensa_pc_in_winexc]
  do_step --> write_dirty
  do_step --> is_stopped[xtensa_is_stopped]

  write_dirty --> reg_get
  write_dirty --> mark_dirty[xtensa_mark_register_dirty]
  write_dirty --> wb_canonical[xtensa_windowbase_offset_to_canonical]
  write_dirty --> queue_exec_ins
  write_dirty --> dm_queue_execute
  write_dirty --> core_status_check

  cause_get --> reg_get
  cause_get --> dm_core_status[xtensa_dm_core_status_get/read]
  core_status_check --> dm_core_status

  reg_get --> reg_get_value[xtensa_reg_get_value]
  reg_set --> reg_set_value[xtensa_reg_set_value]
  reg_set --> reg_get_value
```

## Port order (reverse-topological — leaves first)

1. **`reg_get` / `reg_set`** — register *cache* accessors (read/write
   `reg_list[i].value`, set `.dirty/.valid`). True leaves. **No JTAG.** Testable
   against a cache model with no hardware.
2. **`reg_get_value` / `reg_set_value`** — the per-`struct reg` value get/set the
   above call. (Trivial; fold into #1.)
3. **`cause_get`** — DEBUGCAUSE accessor (reads the cached DEBUGCAUSE reg / core
   status). Depends only on #1 + the DM status read we already have.
4. **`core_status_check`** — already exists in espjtag (verify it matches).
5. **`mark_register_dirty`, `windowbase_offset_to_canonical`** — cache-only
   helpers `write_dirty` needs. Leaves (no JTAG).
6. **`write_dirty_registers`** — flush dirty cache → core (the big one; uses
   `queue_exec_ins`/`dm_queue_execute` we have, + #1/#5).
7. **`do_step`** — single-step (needs #1,#3, `fetch_all_regs`, `pc_in_winexc`,
   and recursively `prepare_resume`/`do_resume` — mind the cycle).
8. **`prepare_resume`** — set PC, DEBUGCAUSE single-step-over-BREAK (#7), write
   hw-breakpoints, `write_dirty` (#6). **This is the step #29 dropped.**
9. **`do_resume`** — `cause_reset` (LX no-op) + RFDO + status check.
10. **`resume` = prepare_resume + do_resume.**
11. **`start_algorithm` / `wait_algorithm`** rebuilt on `resume` + the cache.

Note the cycle `do_step ⇄ prepare_resume ⇄ do_resume`: port the three together as
one unit (they're mutually recursive in the source — don't try to split them).

## Scope decision (locked)

**Full descriptor model.** `write_dirty_registers` (#6) is ported with OpenOCD's
whole register model brought in verbatim: the `xtensa_regs[]` descriptor table,
the `XT_REG_SPECIAL/USER/FR/TIE` type tags, the optregs split, windowbase
canonicalization, and the CPENABLE-delay / MS-after-AR ordering quirks. No subset,
no "only the path the algorithm exercises" shortcut. This adds extra leaves —
`xtensa_regs[]` table, `mark_register_dirty`, `windowbase_offset_to_canonical` —
all ported and tested before #6. Maximally faithful; diffs 1:1 against OpenOCD.

This means a new module holding the faithful cache model (`xtensa_regs[]`,
`struct reg`-equivalent, get/set/dirty), kept separate from the existing
hand-rolled `xtensa.py` NAR layer so the port stays diffable against the source.
