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
    ("Failed to get flash map", "OpenOCD program_esp reads the ESP app/partition "
     "map; we write a RAW test offset (no app image) — benign, flash still writes"),
    ("Application image is invalid", "same: program_esp app-image check on a raw "
     "test offset — expected, the write itself succeeds + is independently verified"),
    ("appimage_offset", "same program_esp app-image note on a raw test offset"),
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
