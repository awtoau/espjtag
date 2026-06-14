#!/usr/bin/env python3
"""flash_matrix.py — flash-speed dump across the WHOLE fleet (C6/C5/C3/S3) and
multiple image SIZES, with per-chip flasher selection.

Fills the gaps in the single-board flash bench: C3 and S3 were missing, and only
one image size was measured. Per chip:
  - RISC-V (C6/C5/C3): espjtag-full + espjtag-incr (JTAG, pure Python) +
    esptool-incr-dev-fast (serial fork) + openocd-full + probers-full, all
    independently verified by espjtag's on-chip CRC read-back.
  - Xtensa (S3): esptool flashers only (espjtag flash execution is open — #29);
    verified by esptool's own post-write MD5 (rc==0).
Offline boards are reported, not silently dropped.

Each cell = median wall-clock of `--rounds` A->B updates (2 sectors changed by
default) at that image size. Dumps a per-chip table + a CSV to tmp/.

Usage: python3 scripts/flash_matrix.py [--sizes 64,256,1024] [--rounds 3]
       [--changed 2] [--addr 0x300000]
"""
import argparse
import os
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flash_bench import (make_ab, run_flasher, ESPTOOL_CHIP, FORK_ESPTOOL,  # noqa: E402
                         DEVPY, SEC, TMP, path_for_serial)
from audit_bench_log import audit                          # noqa: E402

# extend the esptool chip map for the full fleet
ESPTOOL_CHIP.update({"C3": "esp32c3", "S3": "esp32s3"})

RISCV_FLASHERS = ["espjtag-full", "espjtag-incr", "esptool-incr-dev-fast",
                  "openocd-full", "probers-full"]
XTENSA_FLASHERS = ["esptool-full", "esptool-incr-dev-fast"]   # serial; espjtag #29

# ONE log file for everything — full tool stdout/stderr of every invocation,
# appended with a header line per call (not the 120-char tail, not per-file).
LOGFILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "tmp", "flash_matrix_full.log")


def fleet():
    """All boards from dev.py fleet-status --json: (name, CHIP, usb_path|None,
    tty|None, online, serial|None, off_limits). CHIP is the short label
    (C6/C5/C3/S3); serial is the STABLE USB MAC used to re-resolve a board that
    re-enumerated (blocker #2); off_limits is the structured esp32-devices.json
    flag (production boards — respected by default, see --include-off-limits).
    Uses --json, not text parsing, so a newly-tagged off-limits board is picked
    up automatically."""
    import json
    out = subprocess.run(["python3", DEVPY, "fleet-status", "--json"],
                         capture_output=True, text=True).stdout
    data = json.loads(out)
    rows = data if isinstance(data, list) else data.get("boards", data.get("devices", []))
    boards = []
    for r in rows:
        chip = (r.get("chip") or "").replace("esp32", "").upper() or "?"
        boards.append((r.get("name"), chip, r.get("usb_path"), r.get("tty"),
                       bool(r.get("connected")), r.get("serial"),
                       bool(r.get("off_limits"))))
    return boards


def esptool_s3(name, tty, chip, addr, A, B, logfile=None):
    """S3 flash via esptool (serial): write A, time write B, esptool verifies
    (rc==0 = its post-write MD5 passed). Uses the fork's device-diff for the
    incr variant. Returns (elapsed_s | None, ok | None, note). When `logfile`
    is given, the full esptool stdout/stderr is written there."""
    bins = {"esptool-full": "esptool", "esptool-incr-dev-fast": FORK_ESPTOOL}
    binary = bins[name]
    paths = {}
    for k, data in (("A", A), ("B", B)):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False, dir=TMP) as f:
            f.write(data)
            paths[k] = f.name

    def flash(binary_, src, diff=None):
        # --diff-with goes AFTER the positional addr+file: its OptionEatAll
        # greedily eats following args, so placing it before the address makes
        # esptool consume 0x300000 as the diff target (the matrix S3 bug).
        cmd = [binary_, "--chip", "esp32s3", "--port", tty, "--before",
               "default-reset", "--after", "hard-reset", "write-flash",
               "--no-compress", hex(addr), src]    # random imgs don't compress
        if diff:
            cmd += ["--diff-with", diff]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if logfile:
            with open(logfile, "a") as lf:
                lf.write(f"$ {' '.join(cmd)}\n--- rc={r.returncode} ---\n"
                         f"{r.stdout}\n{r.stderr}\n")
        return r

    try:
        r = flash("esptool", paths["A"])              # setup A (stock esptool)
        if r.returncode != 0:
            return None, False, "setup-A failed"
        t0 = time.perf_counter()
        diff = "device" if name == "esptool-incr-dev-fast" else None
        r = flash(binary, paths["B"], diff=diff)
        el = time.perf_counter() - t0
        ok = r.returncode == 0
        return el, ok, "" if ok else (r.stderr or r.stdout)[-100:].strip()
    finally:
        for p in paths.values():
            os.unlink(p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="64,256,1024")     # KiB
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--changed", type=int, default=2)
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=0x300000)
    ap.add_argument("--include-off-limits", action="store_true",
                    help="ALSO flash boards marked off_limits in esp32-devices.json "
                         "(e.g. the production ble_bridge_s3). DEFAULT: skip them — "
                         "flashing overwrites production firmware.")
    args = ap.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]

    boards = fleet()
    print(f"fleet: {sum(b[4] for b in boards)}/{len(boards)} online; "
          f"sizes {sizes} KiB, {args.changed} sectors changed, "
          f"{args.rounds} rounds, verify included")
    if args.include_off_limits:
        print("!!! --include-off-limits: off_limits boards WILL be flashed "
              "(production firmware will be overwritten)")
    open(LOGFILE, "w").close()                          # fresh full log per run
    print(f"full tool output -> {LOGFILE}\n")

    csv = ["board,chip,size_kb,flasher,median_ms,eff_MBps,act_MBps,n,status"]
    for name, chip, usb, tty, online, serial, off_limits in boards:
        riscv = chip in ("C6", "C5", "C3")
        flashers = (RISCV_FLASHERS if riscv else
                    XTENSA_FLASHERS if chip == "S3" else [])
        print(f"=== {name} [{chip}] {'online' if online else 'OFFLINE'} "
              f"{usb or ''} {tty or ''}{' OFF-LIMITS' if off_limits else ''} ===")
        if off_limits and not args.include_off_limits:
            # Respect the production guard by default; flashing overwrites the
            # board's firmware. Opt in with --include-off-limits.
            print("    OFF-LIMITS — skipped (pass --include-off-limits to flash)\n")
            csv.append(f"{name},{chip},,,,,,,off-limits-skipped")
            continue
        if not online:
            print("    offline — skipped\n")
            csv.append(f"{name},{chip},,,,,offline")
            continue
        if not flashers:
            print(f"    no flasher set for {chip}\n")
            continue
        for kb in sizes:
            A, B = make_ab(kb, args.changed)
            cells = {}
            for f in flashers:
                times, fails = [], 0
                for _ in range(args.rounds):
                    # FAIL FAST: one attempt, no retry-masking. After it, audit
                    # this invocation's fresh log slice; ANY warning/error/EXC
                    # aborts the whole run immediately (don't grind 8 boards x 3
                    # sizes to report at the end). Fix the warning, then re-run.
                    el = ok = note = None
                    # Re-resolve usb_path from the STABLE serial NOW (#2): a board
                    # that re-enumerated under hub load keeps its serial but gets a
                    # new path. cur falls back to the scan-time path if the board
                    # isn't found (so the failure is "board gone", not a stale path).
                    cur = (path_for_serial(serial) or usb) if serial else usb
                    with open(LOGFILE, "r+") as lf:
                        mark = lf.seek(0, 2)               # offset before this call
                        lf.write(f"\n##### {name} [{chip}] {kb}KiB {f} "
                                 f"(usb {cur}) #####\n")
                    try:
                        if chip == "S3":
                            el, ok, note = esptool_s3(f, tty, chip, args.addr, A, B,
                                                      logfile=LOGFILE)
                        else:
                            el, ok, note = run_flasher(f, cur, tty, chip, args.addr,
                                                       A, B, logfile=LOGFILE)
                    except Exception as e:                 # noqa: BLE001
                        import traceback
                        el, ok, note = None, False, f"EXC {e}"
                        with open(LOGFILE, "a") as lf:
                            lf.write(traceback.format_exc())
                    # audit ONLY this invocation's output -> abort on any dirt
                    slice_path = LOGFILE + ".slice"
                    with open(LOGFILE) as lf:
                        lf.seek(mark)
                        open(slice_path, "w").write(lf.read())
                    if audit([slice_path]) or ok is False:
                        print(f"\n!!! FAIL-FAST: {name} {chip} {kb}KiB {f} "
                              f"-> aborting. note={note!r}")
                        print(f"!!! full output: {LOGFILE}")
                        return 1
                    if el is not None and ok:
                        times.append(el * 1000)
                    elif ok is None:
                        fails = -1; break                  # skip (e.g. no tty)
                    else:
                        fails += 1
                if times:
                    med = statistics.median(times)
                    # two rates: EFFECTIVE = whole image / time (how fast the
                    # update completes, the user-facing number), ACTUAL =
                    # bytes-actually-written / time (raw throughput). For full
                    # flashers they're equal; for incremental, only `changed`
                    # sectors are written so ACTUAL >> EFFECTIVE.
                    sec = med / 1000.0
                    eff = (kb / 1024.0) / sec                  # MB/s on image size
                    written_mb = (args.changed * SEC / 1e6 if "incr" in f
                                  else kb * 1024 / 1e6)
                    act = written_mb / sec                     # MB/s on bytes written
                    cells[f] = (f"{med:6.0f} ms  {eff:5.2f} MB/s eff "
                                f"{act:5.2f} MB/s act (n={len(times)})")
                    csv.append(f"{name},{chip},{kb},{f},{med:.0f},"
                               f"{eff:.3f},{act:.3f},{len(times)},ok")
                elif fails < 0:
                    cells[f] = "skip (no tty / unsupported)"
                    csv.append(f"{name},{chip},{kb},{f},,,,,skip")
                else:
                    cells[f] = f"FAIL x{fails} — {(note or '?')[:70]}"   # show WHY
                    csv.append(f"{name},{chip},{kb},{f},,,,,fail:{(note or '?')[:60]}")
            print(f"  {kb:5d} KiB:")
            for f in flashers:
                print(f"      {f:28s} {cells.get(f, '-')}")
        print()

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "tmp", "flash_matrix.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w").write("\n".join(csv) + "\n")
    print(f"CSV -> {out}")
    # if we got here, fail-fast never tripped — the whole run was clean
    print("\nALL CLEAN — no tool warnings/errors (fail-fast would have aborted).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
