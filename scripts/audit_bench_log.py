#!/usr/bin/env python3
"""audit_bench_log.py — fail the bench if ANY tool emitted a warning or error.

NOT a catalogue of today's known problems (that's pointless — those get fixed in
the code). This is a GENERIC, forward-looking gate: a healthy benchmark run
produces ZERO warnings/errors from every tool it drives. So we scan the captured
output for generic problem indicators and report every UNIQUE offending line. A
NEW warning that nobody has seen yet trips this automatically next run — which is
the whole point.

When a line legitimately can't be silenced (a third-party tool's cosmetic note),
add it to ALLOW below with a reason — an explicit, reviewed exception, not a
blanket ignore.

Usage: python3 scripts/audit_bench_log.py <logfile> [...]
Exit: 0 = clean; 1 = warnings/errors present.
"""
import re
import sys

# Strong problem indicators (case-insensitive, word-ish boundaries to limit
# false positives). The bench should emit NONE of these in healthy operation.
INDICATORS = re.compile(
    r"\b(?:warn|warning|error|err|fail|failed|failure|traceback|exception|"
    r"deprecat\w*|panic|invalid|timeout|timed out|busy|refused|unable|"
    r"not found|no such|cannot|can't|corrupt|mismatch)\b",
    re.IGNORECASE)

# Reviewed exceptions: substrings that, if present in a line, exempt it. Each
# MUST carry a reason. Keep this list short and justified — every entry is a
# warning we consciously accept, not a blanket mute.
ALLOW = [
    # (substring, reason)
    #
    # OpenOCD program_esp runs `reset init` internally, which ALWAYS inspects the
    # ESP partition table / app image at boot. Our boards carry no valid app image
    # (we only ever flash RAW test offsets), so that inspection fails and warns —
    # before, and independently of, the write we actually do. This is structural,
    # not a bug: confirmed `esp appimage_offset -1` does NOT silence it (no real
    # app exists to point at) and a separate init/halt to suppress it collides with
    # program_esp's own reset init and breaks the flash erase. The write itself
    # succeeds and is INDEPENDENTLY verified by esptool serial readback. So this is
    # a cosmetic third-party boot note that genuinely cannot be silenced at source.
    ("Failed to get flash map", "program_esp boot-time partition inspection; no app "
     "image on a raw test offset — cannot be silenced (appimage_offset -1 tested)"),
    ("Application image is invalid", "same program_esp boot inspection; write "
     "succeeds + is independently verified by esptool serial readback"),
    ("appimage_offset", "same program_esp app-image boot note on a raw test offset"),
    #
    # When the bench alternates espjtag and openocd on one chip, openocd finds
    # espjtag's resident RAM stub (#27) in the stub region (the magic is RISC-V
    # `jal` opcodes, 0x...006F — i.e. real code, not openocd's stub), warns, then
    # installs ITS OWN stub and proceeds. Confirmed self-recovering: the slice
    # that emits this still reports "Programming Finished" + rc=0 and passes the
    # independent verify. Benign cross-tool RAM contention, not a flash failure.
    ("Installed stub code magic_num", "openocd found espjtag's resident RAM stub; "
     "it reinstalls its own and finishes OK (verified) — benign cross-tool note"),
    ("Expected stub code magic_num", "pair of the above; openocd reinstalls its "
     "stub and programming finishes + is independently verified"),
]


def audit(paths):
    text = ""
    for p in paths:
        try:
            text += open(p, errors="replace").read() + "\n"
        except OSError as e:
            print(f"  (cannot read {p}: {e})")
    clean = re.sub(r"\x1b\[[0-9;]*m", "", text)        # strip ANSI colour
    counts = {}
    for line in clean.splitlines():
        s = line.strip()
        if not s or not INDICATORS.search(s):
            continue
        if any(a in s for a, _ in ALLOW):
            continue
        counts[s] = counts.get(s, 0) + 1
    print(f"=== bench-log audit: {', '.join(paths)} ===")
    if not counts:
        print("  CLEAN — no warnings/errors in any tool output.")
        return 0
    print(f"  {len(counts)} UNIQUE warning/error line(s) — the run is NOT clean:")
    for s, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"   x{n:<4d} {s[:140]}")
    print(f"--- VERDICT: DIRTY ({sum(counts.values())} total, "
          f"{len(counts)} unique). Fix the source or add a reviewed ALLOW entry. ---")
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: audit_bench_log.py <logfile> [...]")
        sys.exit(2)
    sys.exit(audit(sys.argv[1:]))
