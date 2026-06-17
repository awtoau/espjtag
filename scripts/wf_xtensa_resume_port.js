export const meta = {
  name: 'xtensa-resume-port',
  description: 'Faithfully port OpenOCD xtensa.c resume/register-cache model into espjtag, leaves-first, verbatim',
  phases: [
    { title: 'Leaves', detail: 'descriptor table + cache get/set + cause_get (no JTAG)' },
    { title: 'WriteDirty', detail: 'write_dirty_registers + its cache helpers' },
    { title: 'ResumeCycle', detail: 'do_step/prepare_resume/do_resume (mutual recursion unit)' },
    { title: 'Algorithm', detail: 'rebuild start/wait_algorithm; design the on-target retest' },
    { title: 'Synthesis', detail: 'reconcile into a single coherent plan + deviations list' },
  ],
}

const SRC = '/mnt/2tb/git_mirror/espressif/openocd-esp32/src/target/xtensa'
const SHA = 'f10eceff22fb8dcd3db69bf3ebc5c70602454af6'
const REPO = '/home/dan/git/awtoau/espjtag'

// Shared rules every porting agent must obey (the doc, inlined so agents can't skip it).
const RULES = `
You are doing a FAITHFUL 1:1 PORT from C (openocd-esp32 @ ${SHA}) to Python, per
${REPO}/docs/FAITHFUL-REIMPLEMENTATION.md. NON-NEGOTIABLE:
- Copy the source VERBATIM. Do not paraphrase, improve, optimise, simplify, or
  reverse-engineer. If your port differs from the source, that difference is a BUG.
- Port the WHOLE function as one unit. Keep steps you don't understand (a flag, an
  extra write, a redundant-looking check) — the author hit a bug you haven't.
- Keep the source's order and structure so the result diffs 1:1 against the C.
- Transcribe constants/tables from the source, never re-derive. Raw bytes stay raw.
- See a faster way? Write a "# TODO: optimise — <what>" comment and move on. NEVER
  optimise inline.
- Every line of your port must trace to a line of the C you can cite (file:line).
- Cite the exact source file:line range you ported from.
The C source is READABLE at ${SRC}/ (xtensa.c, xtensa_regs.h, xtensa.h,
xtensa_debug_module.c/.h). READ THE SOURCE — do not work from memory.
The existing espjtag NAR layer is ${REPO}/espjtag/xtensa.py (write_mem/read_mem,
nar_read/nar_write, _set_ar/_get_ar, _set_sr_a3/_get_sr_a3, DIR0EXEC instruction
injection, the XT_INS_* equivalents). Build the cache model on top of that layer —
do NOT reinvent the transport. Target is ESP32-S3 = Xtensa LX7 (core_type XT_LX);
port the NX branches faithfully but they are not exercised.
Output PYTHON CODE for the ported piece + a "PORTED FROM:" provenance block with
file:line citations + any DEVIATION you were forced to make (call them out loudly).
`

const PORT_SCHEMA = {
  type: 'object',
  required: ['unit', 'python', 'provenance', 'deviations'],
  properties: {
    unit: { type: 'string', description: 'the function/table ported' },
    python: { type: 'string', description: 'the verbatim Python port' },
    provenance: { type: 'string', description: 'source file:line ranges ported from' },
    deviations: {
      type: 'array', items: { type: 'string' },
      description: 'forced deviations from verbatim, each marked; empty if none',
    },
    test: { type: 'string', description: 'a no-hardware test sketch proving fidelity (cache-model level), or "" if N/A' },
  },
}

const VERIFY_SCHEMA = {
  type: 'object',
  required: ['faithful', 'issues'],
  properties: {
    faithful: { type: 'boolean', description: 'true iff the port is a verbatim 1:1 of the cited source' },
    issues: {
      type: 'array', items: { type: 'string' },
      description: 'each place the port paraphrased/dropped/improved vs the C source (file:line), empty if faithful',
    },
    missed_calls: {
      type: 'array', items: { type: 'string' },
      description: 'callees in the C body that the port silently omitted',
    },
  },
}

// Adversarial verify: re-read the C and the port, default to NOT faithful unless proven.
async function verify(port, srcHint) {
  if (!port) return { faithful: false, issues: ['port agent returned null'] }
  return agent(
    `${RULES}\n\nADVERSARIAL FIDELITY CHECK. Here is a proposed Python port of ` +
    `"${port.unit}" (ported from ${srcHint}):\n\n` +
    '```python\n' + port.python + '\n```\n\n' +
    `Claimed provenance: ${port.provenance}\n` +
    `Claimed deviations: ${JSON.stringify(port.deviations)}\n\n` +
    `RE-READ the actual C at ${SRC} for "${port.unit}". Line by line, find EVERY ` +
    `place the Python paraphrased, dropped a step, reordered, simplified, or ` +
    `"improved" vs the C. Pay special attention to steps that "look unnecessary" — ` +
    `those are the ones ports wrongly drop (the #29 bug was a dropped single-step-` +
    `over-BREAK). Default to faithful=false unless the port is a true 1:1. List ` +
    `every callee in the C body the port omitted.`,
    { label: `verify:${port.unit}`, phase: port._phase || 'Leaves', schema: VERIFY_SCHEMA }
  )
}

async function portAndVerify(spec, phase) {
  const port = await agent(
    `${RULES}\n\nPORT THIS UNIT: ${spec.unit}\nSource: ${spec.src}\n${spec.notes || ''}`,
    { label: `port:${spec.unit}`, phase, schema: PORT_SCHEMA }
  )
  if (port) port._phase = phase
  const verdict = await verify(port, spec.src)
  return { spec, port, verdict }
}

// ---- Phase 1: leaves (independent, fan out) --------------------------------
phase('Leaves')
log('Porting the true leaves: descriptor table + reg cache get/set + cause_get')
const LEAVES = [
  { unit: 'xtensa_regs[] descriptor table + struct xtensa_reg_desc + XT_REG_IDX_* enum',
    src: `${SRC}/xtensa.c:178 (table) + ${SRC}/xtensa_regs.h (struct + enum + types)`,
    notes: 'Transcribe the WHOLE table verbatim (name, reg_num, type per entry). This is data — copy every row. Also port the XT_REG_TYPE enum and XT_REG_IDX_* indices it is keyed by.' },
  { unit: 'xtensa_reg_get / xtensa_reg_set / xtensa_reg_get_value / xtensa_reg_set_value (register CACHE accessors, no JTAG)',
    src: `${SRC}/xtensa.c:1129-1143 + the *_value helpers`,
    notes: 'These read/write reg_list[i].value and set .dirty/.valid. A struct reg equivalent: {value, dirty, valid, exist}. NO hardware.' },
  { unit: 'xtensa_cause_get + xtensa_cause_clear (DEBUGCAUSE accessors)',
    src: `${SRC}/xtensa.c:1161-1218`,
    notes: 'LX path returns reg_get(DEBUGCAUSE) from the cache. Port the NX branch faithfully too.' },
  { unit: 'xtensa_mark_register_dirty + xtensa_windowbase_offset_to_canonical (cache helpers write_dirty needs)',
    src: `${SRC}/xtensa.c (grep both names)`,
    notes: 'Cache-only, no JTAG. Needed by write_dirty_registers.' },
]
const leafResults = await parallel(LEAVES.map(s => () => portAndVerify(s, 'Leaves')))

// ---- Phase 2: write_dirty_registers (full descriptor model) ----------------
phase('WriteDirty')
log('Porting write_dirty_registers — FULL descriptor model (scope decision: no subset)')
const leafCode = leafResults.filter(Boolean).filter(r => r.port)
  .map(r => `// ${r.spec.unit}\n${r.port.python}`).join('\n\n')
const writeDirty = await portAndVerify({
  unit: 'xtensa_write_dirty_registers',
  src: `${SRC}/xtensa.c:705-927`,
  notes: 'FULL descriptor model — port the WHOLE function: SFR/USER/FR/TIE dispatch, ' +
    'optregs split, CPENABLE delay, windowbase grab + ARx/Ax canonical reconciliation, ' +
    'MS-after-AR ordering, preserve_a3. Use the queue_exec_ins / dm_queue_execute / ' +
    'core_status_check equivalents from espjtag/xtensa.py. It builds on these already-' +
    `ported leaves:\n${leafCode}`,
}, 'WriteDirty')

// ---- Phase 3: the resume cycle (mutual recursion — ONE unit) ---------------
phase('ResumeCycle')
log('Porting do_step / prepare_resume / do_resume TOGETHER (mutually recursive — one unit)')
const resumeCycle = await portAndVerify({
  unit: 'xtensa_do_step + xtensa_prepare_resume + xtensa_do_resume + xtensa_resume (ONE mutually-recursive unit)',
  src: `${SRC}/xtensa.c:1648-1756 (prepare_resume,do_resume,resume) + 1783-1900 (do_step) + 1711 (cause_reset) + fetch_all_regs + pc_in_winexc + is_stopped`,
  notes: 'Port all four together — they call each other. CRITICAL: prepare_resume MUST keep ' +
    'the DEBUGCAUSE_BI/BN single-step-over-BREAK (xtensa.c:1682) — THAT is the #29 bug that ' +
    'was dropped. do_resume on LX = cause_reset(no-op) + RFDO + status check. Also port ' +
    'fetch_all_regs (populates DEBUGCAUSE in the cache at halt) and pc_in_winexc enough for do_step.',
}, 'ResumeCycle')

// ---- Phase 4: algorithm path + retest design -------------------------------
phase('Algorithm')
log('Rebuilding start/wait_algorithm on the cache+resume, and designing the S3 retest')
const algo = await portAndVerify({
  unit: 'xtensa_start_algorithm + xtensa_wait_algorithm (rebuilt on the register cache + xtensa_resume)',
  src: `${SRC}/xtensa.c:2810-3017`,
  notes: 'Now that resume/cache exist, rebuild these to: backup whole core_cache, set reg_params ' +
    'BY NAME into the cache, set vecbase if trap_entry_addr, then call xtensa_resume(current=false, ' +
    'entry_point, handle_breakpoints=true, debug_execution=true). wait = target_wait_state HALTED + ' +
    'force-halt fallback + pc==exit_point check + copy out params + restore. Replace the current ' +
    'hand-rolled EPC6+RFDO shortcut in espjtag/xtensa.py.',
}, 'Algorithm')
const retest = await agent(
  `${RULES}\n\nDESIGN (don't run — no hardware here) the retest that proves this port fixes #29 ` +
  `on the real S3 (s3-xiao-sense, usb 1-1.3.3.4). We have scripts/test_s3_stub.tcl (on-target via ` +
  `the Tcl bridge) where cmd_test1 passed but flash_map_get hung at the trampoline. Specify: the ` +
  `exact assertions, the mock-level (no-hardware) test that the resume now emits the single-step-` +
  `over-BREAK, and how check.py --real should invoke it. Reference docs/XTENSA-RESUME-PORT-MAP.md.`,
  { label: 'design:retest', phase: 'Algorithm' }
)

// ---- Phase 5: synthesis ----------------------------------------------------
phase('Synthesis')
log('Reconciling all ported units into one coherent integration plan + deviation ledger')
const all = [...leafResults.filter(Boolean), writeDirty, resumeCycle, algo].filter(r => r && r.port)
const bundle = all.map(r =>
  `### ${r.spec.unit}\nPROVENANCE: ${r.port.provenance}\nFAITHFUL: ${r.verdict?.faithful}\n` +
  `ISSUES: ${JSON.stringify(r.verdict?.issues || [])}\nDEVIATIONS: ${JSON.stringify(r.port.deviations)}\n\n` +
  '```python\n' + r.port.python + '\n```'
).join('\n\n---\n\n')
const synthesis = await agent(
  `${RULES}\n\nYou are the integrator. Below are ${all.length} verbatim-ported units (each with an ` +
  `adversarial fidelity verdict) for the Xtensa resume/register-cache model. Produce a SINGLE ` +
  `coherent integration plan for espjtag: which file(s) the cache model lands in (a new module, ` +
  `kept diffable vs the C, separate from the NAR layer in espjtag/xtensa.py), how it wires into the ` +
  `existing transport, the exact order to land + test, and a CONSOLIDATED DEVIATION LEDGER (every ` +
  `place any unit was not 1:1, gathered from the verdicts). Flag any unit whose verdict was ` +
  `faithful=false as MUST-REDO before landing. Do not write new logic — only integrate the ports.\n\n` +
  bundle,
  { label: 'synthesis', phase: 'Synthesis' }
)

return {
  sha: SHA,
  leaves: leafResults.filter(Boolean).map(r => ({ unit: r.spec.unit, faithful: r.verdict?.faithful, issues: r.verdict?.issues })),
  write_dirty: { faithful: writeDirty?.verdict?.faithful, issues: writeDirty?.verdict?.issues },
  resume_cycle: { faithful: resumeCycle?.verdict?.faithful, issues: resumeCycle?.verdict?.issues },
  algorithm: { faithful: algo?.verdict?.faithful, issues: algo?.verdict?.issues },
  retest_design: retest,
  integration_plan: synthesis,
}
