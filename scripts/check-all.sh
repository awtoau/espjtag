#!/usr/bin/env bash
# check-all.sh — THE post-change regression gate. Run after EVERY espjtag change:
#
#   NOTE="batched writes (#22)" scripts/check-all.sh
#
# 1. incremental invariant test        (hardware-free — pyOCD bug class)
# 2. selftest + DMIRESET hammer, C6    (drain_mode=validate byte accounting)
# 3. selftest + DMIRESET hammer, C5    (two-TAP chain)
# 4. xcheck: full 3-way JTAG dump      (espjtag vs OpenOCD vs probe-rs, same silicon)
# 5. flash bench, recorded             (-> tmp/flash-bench.db keyed by git sha)
# 6. progression graph                 (-> docs/images/flash-progression.png)
#
# Speed history: every run lands in the DB under NOTE + sha, so per-fix speed
# progression is `flash_bench.py --report` / `--graph`.
#
# Boards default to the bench fixtures; override via env for other setups.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE" || exit 1
mkdir -p tmp
PY=.venv/bin/python
LOG=tmp/check-all.log
NOTE="${NOTE:-check-all}"
C6_USB="${C6_USB:-1-1.3.1.3.1}"          # c6-xiao-a
C6_MAC="${C6_MAC:-E4:B0:63:41:C1:44}"
C6_TTY="${C6_TTY:-/dev/ttyACM2}"
C5_USB="${C5_USB:-1-1.2}"                # c5-xiao-a
FLASHERS="${FLASHERS:-espjtag-full,espjtag-incr,esptool-incr}"

: > "$LOG"
declare -a names results
step() {  # step <name> <cmd...>
  local name=$1; shift
  echo "=== $name ===" | tee -a "$LOG"
  "$@" >> "$LOG" 2>&1
  local rc=$?
  names+=("$name"); results+=("$rc")
  if [ $rc -eq 0 ]; then echo "  PASS"; else echo "  FAIL (rc=$rc) — see $LOG"; fi
}

step "invariant (hw-free)"  $PY scripts/incremental_invariant_test.py
step "selftest+dmireset C6" $PY scripts/validate_dmireset.py --usb-path "$C6_USB"
step "selftest+dmireset C5" $PY scripts/validate_dmireset.py --usb-path "$C5_USB"
step "xcheck 3-way dump C6" $PY scripts/xcheck.py --usb "$C6_USB" --serial "$C6_MAC"
step "flash bench (record)" $PY scripts/flash_bench.py --usb "$C6_USB" --tty "$C6_TTY" \
                                --rounds 3 --flashers "$FLASHERS" --record --note "$NOTE"
step "progression graph"    $PY scripts/flash_bench.py --graph

fail=0
echo "=== check-all summary (note: $NOTE) ==="
for i in "${!names[@]}"; do
  if [ "${results[$i]}" -eq 0 ]; then s=PASS; else s=FAIL; fail=1; fi
  printf '  %-22s %s\n' "${names[$i]}" "$s"
done
exit $fail
