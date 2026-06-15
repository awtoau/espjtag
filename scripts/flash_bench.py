#!/usr/bin/env python3
"""flash_bench.py — fleet + cross-tool flash benchmark for the incremental engine.

For an A->B update (B = A with a few sectors changed), measure WALL-CLOCK + verify
CORRECTNESS for each flasher, across the connected C6/C5 fleet, N rounds. Shows
espjtag's incremental (write-only-changed) vs full-flash tools.

Flashers (pluggable, --flashers):
  espjtag-full      espjtag.flash_write       (erase+program every sector)
  espjtag-incr      espjtag.flash_incremental (on-chip CRC-32 diff -> write only changed)
  esptool-full      esptool write_flash       (serial, compressed transfer = default)
  esptool-nocomp    esptool write_flash -u    (serial, compression off)
  esptool-incr      esptool write_flash --diff-with <old> (serial incremental)
  openocd-full      openocd-esp32 program_esp (JTAG; no incremental mode exists)
  probers-full      probe-rs download         (JTAG; no per-sector incremental)
  probers-preverify probe-rs download --preverify (whole-image skip-if-same only)

Verification is INDEPENDENT of the flasher: espjtag on-chip CRC-32 read-back of every
sector vs the image. The flasher never certifies its own result.

Usage:
  .venv/bin/python scripts/flash_bench.py --usb 1-1.3.1.3.1 [--kb 64] [--rounds 3] \
        [--changed 2] [--flashers espjtag-full,espjtag-incr] [--tty /dev/ttyACM2]
  .venv/bin/python scripts/flash_bench.py --fleet [--rounds 3]
"""
import argparse
import os
import random
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util

from espjtag import EspUsbJtag, chips

DEVPY = "/home/dan/git/esp32-zephyr/scripts/dev.py"
SEC = 0x1000
ADDR = 0x300000
ESPTOOL_CHIP = {"C6": "esp32c6", "C5": "esp32c5"}
OOCD = "/home/dan/.espressif/tools/openocd-esp32/v0.12.0-esp32-20251215/openocd-esp32"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(ROOT, "tmp")                  # workspace tmp, NOT system /tmp
os.makedirs(TMP, exist_ok=True)


def _resolve_fork_esptool():
    """Locate the esptool FORK the bench compares against (device-diff +
    fast-USJ-reset features). Resolution order, no hard-coded sibling path:
      1) $FORK_ESPTOOL env var (explicit override)
      2) vendored copy in this repo: vendor/esptool-fork/.venv/bin/esptool
      3) legacy sibling checkout ~/git/esptool-fork/.venv/bin/esptool
      4) "esptool" on PATH (LAST resort — may be upstream, not the fork; the
         fork-only rows will then just measure upstream)
    """
    cand = [os.environ.get("FORK_ESPTOOL"),
            os.path.join(ROOT, "vendor", "esptool-fork", ".venv", "bin", "esptool"),
            os.path.expanduser("~/git/esptool-fork/.venv/bin/esptool")]
    for c in cand:
        if c and os.path.exists(c):
            return c
    return "esptool"


FORK_ESPTOOL = _resolve_fork_esptool()
DB = os.path.join(TMP, "flash-bench.db")
GRAPH = os.path.join(ROOT, "docs", "images", "flash-progression.png")


# --- speed-progression DB: every --record'ed run is keyed by git sha + note, so
# --- the per-fix speed history graphs straight out of `--graph` (#22/#36 etc.)
def db_open():
    import sqlite3
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, ts TEXT, "
              "sha TEXT, dirty INT, note TEXT, kb INT, changed INT, entropy TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS results(run_id INT, board TEXT, chip TEXT, "
              "flasher TEXT, round INT, ms REAL, ok INT)")
    return c


def db_record(rows, note, kb, changed, entropy):
    import datetime
    sha = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "-C", ROOT, "status", "--porcelain"],
                                capture_output=True, text=True).stdout.strip())
    c = db_open()
    cur = c.execute("INSERT INTO runs(ts, sha, dirty, note, kb, changed, entropy) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (datetime.datetime.now().isoformat(timespec="seconds"),
                     sha, int(dirty), note, kb, changed, entropy))
    rid = cur.lastrowid
    c.executemany("INSERT INTO results VALUES(?,?,?,?,?,?,?)",
                  [(rid, *r) for r in rows])
    c.commit()
    c.close()
    print(f"recorded run {rid} (sha {sha}{'+dirty' if dirty else ''}, note '{note}') -> {DB}")


def _run_medians(c):
    """[(run-label, {flasher: median-ms})] in chronological order, ok rows only."""
    out = []
    for rid, sha, dirty, note in c.execute("SELECT id, sha, dirty, note FROM runs ORDER BY id"):
        med = {}
        for (f,) in c.execute("SELECT DISTINCT flasher FROM results WHERE run_id=?", (rid,)):
            ts = sorted(ms for (ms,) in c.execute(
                "SELECT ms FROM results WHERE run_id=? AND flasher=? AND ok=1", (rid, f)))
            if ts:
                med[f] = ts[len(ts) // 2]
        out.append((f"{rid}:{sha}{'*' if dirty else ''} {note}", med))
    return out


def cmd_report():
    c = db_open()
    runs = _run_medians(c)
    flashers = sorted({f for _, med in runs for f in med})
    print(f"{'run (sha note)':40s} " + " ".join(f"{f:>16s}" for f in flashers))
    for label, med in runs:
        print(f"{label[:40]:40s} " + " ".join(
            f"{med[f]:14.0f}ms" if f in med else f"{'—':>16s}" for f in flashers))
    c.close()


def cmd_graph():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    c = db_open()
    runs = _run_medians(c)
    c.close()
    if not runs:
        print("no recorded runs"); return
    flashers = sorted({f for _, med in runs for f in med})
    fig, ax = plt.subplots(figsize=(11, 6))
    xs = range(len(runs))
    for f in flashers:
        ys = [med.get(f) for _, med in runs]
        ax.plot(xs, ys, marker="o", label=f)
    ax.set_xticks(list(xs))
    ax.set_xticklabels([lab for lab, _ in runs], rotation=30, ha="right", fontsize=7)
    ax.set_ylabel("median ms (lower = faster)")
    ax.set_title("flash speed progression per fix (64 KiB A->B, see runs table)")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    os.makedirs(os.path.dirname(GRAPH), exist_ok=True)
    fig.savefig(GRAPH, dpi=110)
    print(f"wrote {GRAPH}")


def make_ab(kb, nchanged, entropy="random"):
    n = kb * 1024
    rnd = random.Random(0xBEEF)
    if entropy == "firmware":
        # compressible stand-in for real firmware: each 16-byte unit = 4 random
        # bytes x4 (zlib gets ~4x) — random data would make any compression
        # comparison meaningless.
        q = rnd.randbytes(n // 4)
        A = bytearray(b"".join(q[i:i + 4] * 4 for i in range(0, len(q), 4)))
    else:
        A = bytearray(rnd.randbytes(n))
    B = bytearray(A)
    nsec = n // SEC
    for k in range(nchanged):                          # spread the changes out
        B[((k * nsec) // nchanged) * SEC + 0x10] ^= 0xFF
    return bytes(A), bytes(B)


def usb_serial(usb_path):
    """USB serial string (the MAC) for probe-rs --probe pinning."""
    import usb.core
    for d in usb.core.find(find_all=True, idVendor=0x303A, idProduct=0x1001):
        if f"{d.bus}-" + ".".join(str(p) for p in (d.port_numbers or ())) == usb_path:
            return usb.util.get_string(d, d.iSerialNumber)
    return None


def path_for_serial(serial):
    """Live usb_path for a STABLE serial (the MAC), re-read from the bus NOW.
    Returns None if the board isn't currently present. This is the fix for the
    matrix drop (#2): a board that re-enumerated under hub load keeps its serial
    but gets a new path, so re-resolving here finds it instead of failing on the
    stale path cached at fleet-scan time. Serial match is colon/case tolerant."""
    import usb.core
    from espjtag.transport import norm_serial
    want = norm_serial(serial)
    for d in usb.core.find(find_all=True, idVendor=0x303A, idProduct=0x1001):
        try:
            if norm_serial(usb.util.get_string(d, d.iSerialNumber)) == want:
                return f"{d.bus}-" + ".".join(str(p) for p in (d.port_numbers or ()))
        except (usb.core.USBError, ValueError):
            continue
    return None


def connect(usb_path):
    try:
        j = EspUsbJtag(usb_path)
    except usb.core.USBError as e:
        # [Errno 16] Resource busy = a leaked libusb interface claim survived a
        # previous process (e.g. a benchmark killed mid-flash). The kernel holds
        # the claim until the device is reset — so reset it and retry once.
        if e.errno != 16:
            raise
        dev = next((d for d in usb.core.find(find_all=True, idVendor=0x303A,
                                             idProduct=0x1001)
                    if f"{d.bus}-" + ".".join(str(p) for p in (d.port_numbers or ()))
                    == usb_path), None)
        if dev is not None:
            try:
                dev.reset()
            except usb.core.USBError:
                pass
        j = EspUsbJtag(usb_path)
    j.examine()
    if not j.halt():
        raise RuntimeError("halt failed")
    return j


def verify_crc(j, addr, B):
    """INDEPENDENT correctness check: on-chip CRC-32 each sector of flash vs B.
    Re-arm the ROM flash gate first via the #33 RECOVERY LADDER — the gate is
    cold after a reset (esptool/openocd both reset), and on the C5 a single
    flash_init isn't always enough (clk+attach+config_param retried), so the old
    one-shot init flaked the verify intermittently (the 'openocd-full FAIL x2'
    matrix artifact)."""
    j._ensure_flash_ready(lambda m: None, "verify")
    host = j._crc_host()
    return all(j.flash_crc_region(addr + s * SEC, SEC) == host(B[s * SEC:(s + 1) * SEC])
               for s in range(len(B) // SEC))


# esptool serial errors split by what retrying actually does (#43):
#
# CONTENTION — the serial port is owned/locked by ANOTHER process, or we lack
# permission. Retrying just RACES the other owner again; it never recovers. Report
# it as contention so the failure is diagnosed, not silently burned through 3 tries.
# (This is the case the "another process took the device" incident actually was.)
_ESPTOOL_CONTENTION = (
    "could not open", "permission denied", "resource busy", "device or resource busy",
    "access is denied", "in use",
)
# TRANSIENT — the CHIP itself isn't serial-ready yet (mid-reboot right after the
# JTAG flash, a momentary sync glitch). Retrying genuinely helps: the chip settles.
# A byte MISMATCH is in NEITHER list — that's a real bad write, returned immediately.
_ESPTOOL_TRANSIENT = (
    "stopped responding", "serial data stream stopped", "serial noise",
    "failed to connect", "no serial data received", "wrong boot mode",
    "timed out waiting",
)


def verify_independent(tty, chip, addr, B, logfile=None, tries=3):
    """INDEPENDENT cross-tool verify: read the flashed region back with esptool
    over SERIAL and compare to B. The strongest correctness check for an espjtag
    flash — espjtag writes over JTAG, esptool reads over a totally separate
    transport + codebase, so a shared bug can't self-certify. Returns (ok, note).
    (probe-rs XIP read returns 0addbad0 = unmapped; openocd flash read is
    chip-flaky on the C5 — esptool serial read is the reliable independent path.)

    Retries TRANSIENT serial-connection failures (contended hub / chip not yet
    serial-ready after the JTAG flash) up to `tries` — but a byte MISMATCH or any
    non-transient error returns immediately, so a real bad write is never masked."""
    if not tty:
        return None, "no tty for independent verify"
    espchip = ESPTOOL_CHIP.get(chip, "auto")
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False, dir=TMP) as f:
        rb = f.name
    try:
        last = "?"
        for attempt in range(tries):
            cmd = ["esptool", "--chip", espchip, "--port", tty, "--before",
                   "default-reset", "--after", "hard-reset", "read-flash",
                   hex(addr), str(len(B)), rb]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            if logfile:
                with open(logfile, "a") as lf:
                    lf.write(f"$ {' '.join(cmd)}\n--- independent verify "
                             f"rc={r.returncode} (attempt {attempt + 1}/{tries}) "
                             f"---\n{r.stdout}\n{r.stderr}\n")
            if r.returncode == 0:
                got = open(rb, "rb").read()
                if got == B:
                    return True, ""
                return False, "independent readback != written image"  # REAL fail
            out = (r.stderr + r.stdout).lower()
            last = f"esptool read-flash rc={r.returncode}"
            if any(c in out for c in _ESPTOOL_CONTENTION):
                # Port owned by another process / no permission. Retrying just
                # races the other owner — report it as contention instead (#43).
                return False, (f"{last}: serial port CONTENTION (another process "
                               f"owns {tty}? or permissions) — not retried")
            if not any(t in out for t in _ESPTOOL_TRANSIENT):
                return False, last                       # non-transient — don't retry
            # transient (chip mid-reboot / sync glitch) -> loop and retry; it settles
        return False, f"{last} (transient, {tries} tries)"
    finally:
        os.unlink(rb)


def verify_after_external(usb_path, addr, B, tries=3):
    """Reconnect + verify after an external flasher's reset, retrying the whole
    connect/halt/verify on transient post-reset failures (the app is rebooting;
    halt can catch it mid-boot). Returns (ok, note)."""
    last = ""
    for _ in range(tries):
        try:
            j = connect(usb_path)
            try:
                return verify_crc(j, addr, B), ""
            finally:
                try:
                    j.resume()
                finally:
                    usb.util.dispose_resources(j.dev)
        except Exception as e:                                 # noqa: BLE001
            last = f"verify-reconnect: {e}"
    return None, last


def setup_a(usb_path, addr, A):
    """Write image A via espjtag (setup step). ALWAYS dispose the USB handle —
    a leaked claim poisons every later connect in this process (Resource busy).
    One retry with flash_init: a preceding external tool (OpenOCD's stub) can
    leave the ROM flash state needing re-init, failing the first erase."""
    for attempt in (0, 1):
        j = connect(usb_path)
        try:
            if attempt:
                j.flash_init()
            j.flash_write(addr, A, verify=False)
            return
        except RuntimeError:
            if attempt:
                raise
        finally:
            try:
                j.resume()
            finally:
                usb.util.dispose_resources(j.dev)


def _wlog(logfile, text):
    """Append text to a per-flasher log file when one was provided."""
    if logfile:
        with open(logfile, "a") as f:
            f.write(text)


# Chips whose MSPI (flash) clock is NOT preserved across a JTAG/SoC reset, so
# OpenOCD's program_esp can't probe flash on a back-to-back invocation ("Failed
# to read flash size!" -> "size 0 KB"). Confirmed on the C5: the FIRST openocd
# run after firmware booted works; the SECOND fails because esp32c5_soc_reset
# leaves the MSPI clock source unset (normal boot does bootloader_init_mspi_clock,
# the reset proc doesn't). esptool over SERIAL hard-resets -> ROM boot re-inits
# the MSPI clock, restoring flash access — proven on hardware. NOT reinventing:
# this is the bootloader's own clock-init path, triggered the documented way.
MSPI_HEAL_CHIPS = {"C5"}


def heal_mspi_clock(tty, chip, logfile=None):
    """Boot firmware over SERIAL so the ROM re-inits the MSPI (flash) clock that a
    prior JTAG/SoC reset left dead — otherwise OpenOCD's next flash probe sees
    0 KB. A cheap esptool flash-id does it (its default/hard reset boots the ROM).
    No-op for chips that preserve the clock across reset (see MSPI_HEAL_CHIPS)."""
    if chip not in MSPI_HEAL_CHIPS or not tty:
        return
    espchip = ESPTOOL_CHIP.get(chip, "auto")
    # --after no-reset is load-bearing: esptool's stub boots and inits the MSPI
    # clock, and NOT hard-resetting at the end leaves that clock LIVE when openocd
    # attaches. A final hard-reset re-kills the clock (openocd's own soc_reset
    # preserves a running MSPI clock but can't init a dead one) -> probe sees
    # 0 KB. Proven: hard-reset gives flaky pass/fail, no-reset gives 3/3 clean.
    cmd = ["esptool", "--chip", espchip, "--port", tty, "--before",
           "default-reset", "--after", "no-reset", "flash-id"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    _wlog(logfile, f"$ {' '.join(cmd)}\n--- MSPI-clock heal rc={r.returncode} ---\n"
                   f"{r.stdout}\n{r.stderr}\n")


def _espjtag_reset(usb_path, logfile=None):
    """Boot the chip into its app over JTAG (espjtag ndmreset) — used to leave a
    known-good RUNNING state after the no-reset MSPI heal. Replaces the old
    esptool --after hard-reset, whose RTS reset RE-ENUMERATES the USB-Serial/JTAG
    peripheral (esptool #970; openocd-esp32 #316/#342) and caused the next tool's
    USB descriptor read to race (libusb -9). espjtag's ndmreset is a full-system
    reset that re-straps + boots the app WITHOUT dropping USB — same device, same
    bus address, no race."""
    from espjtag.reset import reset_run
    try:
        reset_run(usb_path)
        _wlog(logfile, f"$ espjtag.reset_run({usb_path})\n--- ndmreset boot OK "
                       f"(no USB re-enumeration) ---\n")
    except Exception as e:                                  # noqa: BLE001
        _wlog(logfile, f"$ espjtag.reset_run({usb_path})\n--- FAILED: {e} ---\n")


def run_flasher(name, usb_path, tty, chip, addr, A, B, logfile=None):
    """Put A on flash (setup), TIME flashing B with `name`, verify B independently.
    Returns (elapsed_s | None, ok | None, note). When `logfile` is given, the
    full external-tool stdout/stderr (or the espjtag exception) is written there
    — so a FAIL is never just a 120-char tail."""
    if name.startswith("espjtag"):
        j = connect(usb_path)
        try:
            j.flash_write(addr, A, verify=False)               # setup: A on flash
            t = time.perf_counter()
            if name == "espjtag-full":
                j.flash_write(addr, B, verify=False)
            else:
                j.flash_incremental(addr, B, verify=False)
            elapsed = time.perf_counter() - t
        finally:
            j.resume()
            usb.util.dispose_resources(j.dev)
        # INDEPENDENT verify: espjtag wrote over JTAG; esptool reads back over
        # SERIAL and compares — no self-certification. (Needs the espjtag USB
        # handle disposed first so esptool can take the serial port.)
        ok, note = verify_independent(tty, chip, addr, B, logfile=logfile)
        return elapsed, ok, note
    cmd, note, env_extra = external_cmd(name, usb_path, tty, chip, addr)
    if cmd is None:
        return None, None, note
    setup_a(usb_path, addr, A)
    if name == "openocd-full":
        # setup_a wrote A over espjtag JTAG, which (like any JTAG/SoC reset) can
        # leave the C5 MSPI flash clock dead -> openocd's probe would see 0 KB.
        # Boot firmware over serial first to re-init the clock the documented way.
        heal_mspi_clock(tty, chip, logfile=logfile)
    paths = {}
    for key, data in (("A", A), ("B", B)):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False, dir=TMP) as f:
            f.write(data)
            paths[key] = f.name
    argv = [a.format(A=paths["A"], B=paths["B"]) for a in cmd]
    t = time.perf_counter()
    # timeout: guards a hung external tool (USB wedge) — generous ceiling, a
    # 64 KiB flash is seconds for every tool here; expiry fails the round.
    r = subprocess.run(argv, capture_output=True, text=True, timeout=180,
                       env={**os.environ, **env_extra} if env_extra else None)
    elapsed = time.perf_counter() - t
    _wlog(logfile, f"$ {' '.join(argv)}\n--- rc={r.returncode} ({elapsed*1000:.0f} ms) ---\n"
                   f"{r.stdout}\n{r.stderr}\n")
    for p in paths.values():
        os.unlink(p)
    if name == "openocd-full":
        # openocd's program_esp leaves the chip halted/reset; the NEXT flasher's
        # espjtag setup_a (ROM flash gate) can then fail its first erase ('flash
        # erase block N -> 1' on the C6). Boot the app over JTAG (espjtag
        # ndmreset, NO USB re-enumeration) to leave a known-good RUNNING chip so
        # the next setup_a + this call's verify both start clean — without the
        # ~450ms USB re-enumeration an esptool hard-reset would cost. (On the C5
        # this also pairs with the no-reset MSPI heal above.)
        _espjtag_reset(usb_path, logfile=logfile)
    if r.returncode != 0:
        return elapsed, False, f"rc={r.returncode}: {(r.stderr or r.stdout)[-120:].strip()}"
    ok, note = verify_after_external(usb_path, addr, B)         # retries post-reset
    return elapsed, ok, note


def external_cmd(name, usb_path, tty, chip, addr):
    """argv template for an external flasher writing {B} at addr ({A} = old image).
    Returns (argv | None, skip-note, extra-env).

    esptool FEATURE-FLAG matrix (fork = ~/git/esptool-fork bench-combo, own venv;
    each fork feature is independently toggleable, so progression graphs can
    isolate them — stock rows always use the system esptool):
      esptool-full / -nocomp / -incr   system esptool (upstream baseline)
      esptool-incr-dev                 fork: device-diff ONLY (reset pinned stock
                                       via ESPTOOL_CFGFILE -> esptool_stock_reset.cfg)
      esptool-full-fast                fork: fast-USJ-reset ONLY (full write)
      esptool-incr-dev-fast            fork: BOTH features
    """
    if name.startswith("esptool"):
        if not tty:
            return None, "skip (no tty)", None
        fork = name in ("esptool-incr-dev", "esptool-incr-dev-fast", "esptool-full-fast")
        env = None
        if name == "esptool-incr-dev":             # isolate device-diff: stock reset
            env = {"ESPTOOL_CFGFILE": os.path.join(ROOT, "scripts",
                                                   "esptool_stock_reset.cfg")}
        # non-deprecated names (default-reset/hard-reset/write-flash) — the
        # underscore forms warn on esptool 5.x and trip the fail-fast audit.
        # --no-compress always: our test images are RANDOM (incompressible), so
        # esptool prints a "cannot compress" Note otherwise (also audit dirt).
        cmd = [FORK_ESPTOOL if fork else "esptool",
               "--chip", ESPTOOL_CHIP.get(chip, "auto"), "--port", tty,
               "--before", "default-reset", "--after", "hard-reset", "write-flash",
               "--no-compress"]
        cmd += [hex(addr), "{B}"]
        if name == "esptool-incr":                 # serial incremental: diff vs old image
            cmd += ["--diff-with", "{A}"]
        elif name in ("esptool-incr-dev", "esptool-incr-dev-fast"):
            cmd += ["--diff-with", "device"]       # serial incremental: on-chip MD5 diff
        return cmd, "", env
    if name == "openocd-full":
        if not os.path.exists(f"{OOCD}/bin/openocd"):
            return None, "skip (no openocd-esp32)", None
        # program_esp is the command that actually WORKS on the fleet (it
        # auto-detects the flash; plain `flash write_image` fails to probe the
        # C5 — 'size 0 KB'). It emits BENIGN app-image warnings when writing a
        # raw test offset (no app image there) — those are ALLOW-listed in
        # audit_bench_log.py with a reason, not a real problem.
        # Disable the gdb/tcl/telnet servers: a one-shot program_esp never uses
        # them, and a prior invocation's gdb server on :3333 can still hold the
        # port when the next starts ("couldn't bind gdb to socket on port 3333:
        # Address already in use" -> OpenOCD init failed). Turning them off
        # removes the host-side port race entirely (not a chip problem).
        return [f"{OOCD}/bin/openocd", "-s", f"{OOCD}/share/openocd/scripts",
                "-c", "gdb port disabled", "-c", "tcl port disabled",
                "-c", "telnet port disabled",
                "-c", f"adapter usb location {usb_path}",
                "-f", f"board/esp32{chip.lower()}-builtin.cfg",
                "-c", "program_esp {B} " + hex(addr) + " exit"], "", None
    if name.startswith("probers"):
        serial = usb_serial(usb_path)
        if not serial:
            return None, "skip (no usb serial)", None
        cmd = ["probe-rs", "download", "--chip", f"esp32{chip.lower()}",
               "--probe", f"303a:1001:{serial}",
               "--binary-format", "bin", "--base-address", hex(addr)]
        if name == "probers-preverify":            # its only skip-if-same mode (whole image)
            cmd += ["--preverify"]
        return cmd + ["{B}"], "", None
    return None, f"unknown flasher {name}", None


def fleet_riscv():
    """Connected esp32c6/c5 from dev.py fleet-status -> [(name, chip, usb_path, tty)]."""
    out = subprocess.run(["python3", DEVPY, "fleet-status"], capture_output=True,
                         text=True).stdout
    boards = []
    for line in out.splitlines():
        if "●" not in line:                               # ● connected only
            continue
        m = re.search(r"(\S+)\s+esp32(c[56])\s+(\d[\d.-]+)", line)  # path = bus-port.port...
        if not m:
            continue
        tty = re.search(r"/dev/ttyACM\d+", line)
        boards.append((m.group(1), m.group(2).upper(), m.group(3),
                       tty.group(0) if tty else None))
    return boards


def bench_board(name, chip, usb, tty, flashers, addr, kb, changed, rounds, log,
                entropy="random"):
    A, B = make_ab(kb, changed, entropy)
    log(f"\n=== {name} [{chip}] {usb} — {kb} KiB, {changed}/{kb*1024//SEC} sectors "
        f"changed, {rounds} rounds ===")
    agg = {f: [] for f in flashers}
    fails = 0
    rows = []                                  # (board, chip, flasher, round, ms, ok)
    for rnd in range(rounds):
        for f in flashers:
            try:
                el, ok, note = run_flasher(f, usb, tty, chip, addr, A, B)
            except Exception as e:                             # noqa: BLE001
                el, ok, note = None, False, f"EXC {e}"
            tag = "PASS" if ok else ("skip" if ok is None and "skip" in note else "FAIL")
            if tag == "FAIL":
                fails += 1
            if el is not None and ok:
                agg[f].append(el)
            if el is not None:
                rows.append((name, chip, f, rnd, el * 1000, int(bool(ok))))
            log(f"  r{rnd} {f:14s} {tag}  "
                f"{f'{el*1000:7.0f} ms' if el is not None else '   —   '}"
                f"{('  ' + note) if note else ''}")
    log(f"  -- {name} medians --")
    for f in flashers:
        ts = sorted(agg[f])
        med = ts[len(ts) // 2] if ts else None
        log(f"     {f:14s} {f'{med*1000:7.0f} ms (n={len(ts)})' if med else 'no data'}")
    if agg.get("espjtag-full") and agg.get("espjtag-incr"):
        ff = sorted(agg["espjtag-full"])[len(agg["espjtag-full"]) // 2]
        fi = sorted(agg["espjtag-incr"])[len(agg["espjtag-incr"]) // 2]
        log(f"     -> incremental speedup vs full: {ff/fi:.2f}x")
    return fails, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb")
    ap.add_argument("--tty")
    ap.add_argument("--fleet", action="store_true")
    ap.add_argument("--kb", type=int, default=64)
    ap.add_argument("--changed", type=int, default=2)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=ADDR)
    ap.add_argument("--flashers", default="espjtag-full,espjtag-incr")
    ap.add_argument("--entropy", choices=("random", "firmware"), default="random")
    ap.add_argument("--record", action="store_true",
                    help="record results into tmp/flash-bench.db keyed by git sha")
    ap.add_argument("--note", default="")
    ap.add_argument("--report", action="store_true", help="print speed progression table")
    ap.add_argument("--graph", action="store_true", help="write docs/images/flash-progression.png")
    args = ap.parse_args()
    if args.report:
        cmd_report(); return 0
    if args.graph:
        cmd_graph(); return 0
    flashers = [f.strip() for f in args.flashers.split(",") if f.strip()]

    if args.fleet:
        boards = fleet_riscv()
        print(f"fleet: {len(boards)} connected C6/C5 -> "
              f"{', '.join(b[0] for b in boards)}")
    elif args.usb:
        ic = None
        j = EspUsbJtag(args.usb)
        ic = j.read_idcode()
        usb.util.dispose_resources(j.dev)
        chip = (chips.lookup(ic) or {}).get("name", "?")
        boards = [("board", chip, args.usb, args.tty)]
    else:
        print("need --usb or --fleet"); return 2

    total_fail = 0
    all_rows = []
    for name, chip, usb_path, tty in boards:
        fails, rows = bench_board(name, chip, usb_path, tty, flashers, args.addr,
                                  args.kb, args.changed, args.rounds, print,
                                  args.entropy)
        total_fail += fails
        all_rows += rows
    print(f"\n=== fleet bench done: {total_fail} failure(s) across "
          f"{len(boards)} board(s) x {args.rounds} round(s) ===")
    if args.record and all_rows:
        db_record(all_rows, args.note, args.kb, args.changed, args.entropy)
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
