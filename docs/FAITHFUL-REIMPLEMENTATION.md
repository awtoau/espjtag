# Rules for porting reference code

When reimplementing working code from [OpenOCD](https://github.com/espressif/openocd-esp32),
[probe-rs](https://github.com/probe-rs/probe-rs), or any battle-tested reference:
copy it 1:1. The reference is correct; you don't yet know why. Copy first,
understand later.

**The rule: if your port differs from the source, that difference is a bug until
proven otherwise.**

## Order of work — leaves first, not top-down

- Read all of it before porting any of it. Map what the top-level function depends
  on — the support/helper functions, the structs.
- Port the leaves first: the small helpers with no dependencies, each a piece you
  can test on its own. Build up to the top from verified pieces — never down from
  an unverified top. Top-down forces you to model the whole call tree in your head,
  which is when you start guessing.
- Be extra careful bringing each helper in: check it against its source before
  anything depends on it. A wrong leaf corrupts everything above it silently.
- A structure you find intriguing — port it as an isolated leaf and test it, don't
  analyse it in place.

## Before you port

- Find the exact upstream function, and the SHA it lives at. Read *that*, not your
  memory of how it works.
- Port the whole function as one unit. Don't start until you have all of it.

## While porting

- Every line of your port must trace to a line of the source you can point to. If
  you're writing from a mental model instead of the text, you're paraphrasing —
  reread the source.
- Keep steps you don't understand. A flag, an extra write, a redundant-looking
  check: the author hit a bug you haven't. Don't drop it. Don't simplify it.
- Don't drop anything in the first pass. Copy it all, working, *then* remove
  things — never skip a step on the way in.
- Keep the source's order and structure (e.g. it may write special registers
  before general ones for a hazard you can't see). Matching structure is what
  lets you diff against the source later.
- Constants and tables: transcribe from the source, never re-derive from a
  datasheet. Raw bytes stay raw — if the reference doesn't parse a blob, you
  don't either.
- Don't decode data the reference treats as opaque. If it passes a blob/struct
  around without inspecting the bytes, don't disassemble or reverse-engineer to
  work out what's inside before writing your code — that's always wasted; if the
  reference doesn't need to know, neither do you. The meaning becomes clear later,
  or never matters.
- See a faster way? Write `# TODO: optimise — <what>` and move on. Never optimise
  inline during a port.

## Before claiming done

- Diff your port against the source. If they've diverged so far you can't, you
  paraphrased — redo it matching structure.
- If it has a bug: "I copied it verbatim" and "it has a bug" can't both be true.
  Re-read the source line-by-line against your code before blaming the hardware.
- Prove fidelity: a no-hardware mock/golden test that your operation *sequence*
  matches the reference's (OpenOCD `dummy`, probe-rs `FakeProbe`), and where
  possible a differential test against the running reference.
- Record the upstream SHA you ported from, in the code.

When in doubt: copy.
