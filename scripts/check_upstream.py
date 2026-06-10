#!/usr/bin/env python3
"""check_upstream.py -- upstream-drift go/no-go for espjtag's ported constants.

espjtag hand-transcribes a handful of numeric values from upstream:
  - openocd-esp32  src/jtag/drivers/esp_usb_jtag.c   (USB-JTAG command nibbles)
  - openocd-esp32  src/jtag/drivers/bitq.c           (the TAP scan model)
  - openocd-esp32  tcl/target/esp32c6.cfg            (WDT-disable + SoC-reset regs
                                                      -> chips.py wdt/reset dicts)
  - riscv          src/target/riscv/debug_defines.h  (DM register + field offsets)

These are pinned in ../upstream.lock at a specific commit SHA. This script
re-fetches each file at the *current* HEAD of the upstream default branch,
re-extracts ONLY the symbols we depend on (not the whole file), and compares
them to the pinned `expected` values:

    GO     nothing we depend on moved -> our ports stay valid (exit 0)
    NO-GO  a depended-on value changed OR a tracked symbol vanished
           -> a human must review the port (exit 1)

It also reports whether the upstream HEAD SHA has advanced past the pinned SHA.
That alone is informational (GO): upstream churns constantly; we only fail when
a value WE depend on actually changed.

Offline / rate-limited / 404: prints a clear UNKNOWN and exits 2 (not 0, not 1)
so CI can tell "couldn't check" from "checked, all good" / "checked, drifted".
Runnable locally:  python3 scripts/check_upstream.py
Uses `gh api` if available (auth + higher rate limit), else plain HTTPS.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# ESPJTAG_LOCK lets tests point at an alternate manifest; default is the repo's.
LOCK_PATH = os.environ.get(
    "ESPJTAG_LOCK", os.path.join(HERE, os.pardir, "upstream.lock"))

EXIT_GO = 0
EXIT_NOGO = 1
EXIT_UNKNOWN = 2


class FetchError(Exception):
    """Network/availability failure -> UNKNOWN, not NO-GO."""


def _have_gh():
    return shutil.which("gh") is not None


def _gh_raw(repo, path, ref):
    """Fetch a file's raw bytes via the authenticated gh CLI."""
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/{path}?ref={ref}",
         "-H", "Accept: application/vnd.github.raw"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise FetchError(f"gh api {repo}/{path}@{ref}: {out.stderr.strip()}")
    return out.stdout


def _https_raw(repo, path, ref):
    """Fetch a file's raw text from raw.githubusercontent.com."""
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise FetchError(f"GET {url}: {e}")


def fetch_file(repo, path, ref, use_gh):
    return _gh_raw(repo, path, ref) if use_gh else _https_raw(repo, path, ref)


def _gh_head_sha(repo, branch, use_gh):
    """Current SHA of the default branch (informational)."""
    if use_gh:
        out = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{branch}", "--jq", ".sha"],
            capture_output=True, text=True,
        )
        if out.returncode == 0:
            return out.stdout.strip()
        return None
    try:
        out = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{repo}.git", branch],
            capture_output=True, text=True,
        )
        if out.returncode == 0 and out.stdout:
            return out.stdout.split()[0]
    except OSError:
        pass
    return None


def _as_int(token):
    """Parse a C integer literal: 0x1f, 31, 0x1fULL, 7ULL."""
    t = token.strip().rstrip("Ll").rstrip("Uu").rstrip("Ll")
    return int(t, 0)


def extract(text, sym):
    """Return (ok, found_value, detail) for one tracked symbol against `text`."""
    m = re.search(sym["regex"], text)
    if not m:
        return False, None, "symbol not found upstream (renamed/removed?)"
    captured = m.group(1).strip()
    kind = sym["kind"]
    if kind == "define_int":
        try:
            val = _as_int(captured)
        except ValueError:
            return False, captured, f"could not parse int from {captured!r}"
        ok = val == sym["expected"]
        return ok, val, ("" if ok else f"expected {sym['expected']}, got {val}")
    if kind == "macro_line":
        # Compare normalised whitespace of the macro body.
        norm = " ".join(captured.split())
        exp = " ".join(str(sym["expected"]).split())
        ok = norm == exp
        return ok, norm, ("" if ok else f"expected {exp!r}, got {norm!r}")
    return False, captured, f"unknown symbol kind {kind!r}"


def main():
    with open(LOCK_PATH) as f:
        lock = json.load(f)

    use_gh = _have_gh()
    print(f"espjtag upstream-drift check  (fetch via {'gh api' if use_gh else 'https'})")
    print("=" * 72)

    # source-key -> (repo, file-rel-path) cache so we fetch each blob once.
    fetch_cache = {}
    nogo = []            # (source, file, symbol, detail)
    unknown = []         # fetch failures
    checked = 0

    for src_name, src in lock["sources"].items():
        repo = src["repo"]
        branch = src.get("default_branch", "master")
        pinned = src["pinned_sha"]

        head = _gh_head_sha(repo, branch, use_gh)
        if head and head != pinned:
            print(f"\n[{src_name}] {repo}@{branch}")
            print(f"  pinned  {pinned}")
            print(f"  HEAD    {head}   (advanced -- informational, "
                  f"not a failure on its own)")
        elif head:
            print(f"\n[{src_name}] {repo}@{branch}  HEAD == pinned {pinned[:12]}")
        else:
            print(f"\n[{src_name}] {repo}@{branch}  (could not read HEAD SHA)")

        for fname, finfo in src["files"].items():
            path = finfo["path"]
            syms = finfo.get("symbols", [])
            if not syms:
                print(f"  {fname}: tracked by SHA only "
                      f"({finfo.get('note', '').split('.')[0]})")
                continue
            key = (repo, path)
            if key not in fetch_cache:
                try:
                    fetch_cache[key] = fetch_file(repo, path, branch, use_gh)
                except FetchError as e:
                    fetch_cache[key] = e
            text = fetch_cache[key]
            if isinstance(text, FetchError):
                print(f"  {fname}: FETCH FAILED -- {text}")
                unknown.append((src_name, fname, str(text)))
                continue

            ok_count = 0
            for sym in syms:
                checked += 1
                ok, got, detail = extract(text, sym)
                if ok:
                    ok_count += 1
                else:
                    nogo.append((src_name, fname, sym["name"], detail,
                                 sym.get("depends", ""), got, sym.get("expected")))
            print(f"  {fname}: {ok_count}/{len(syms)} tracked symbols match "
                  f"upstream {branch}")

    print("\n" + "=" * 72)

    if unknown:
        print(f"RESULT: UNKNOWN -- {len(unknown)} source(s) could not be fetched "
              f"(offline / rate-limited / moved).")
        for src, fname, why in unknown:
            print(f"  ? {src}/{fname}: {why}")
        if not nogo:
            print("No drift detected in what WAS fetched, but the check is "
                  "incomplete. Re-run when online.")
            return EXIT_UNKNOWN

    if nogo:
        print(f"RESULT: NO-GO -- {len(nogo)} depended-on upstream value(s) "
              f"changed. Review the port before trusting it.")
        for src, fname, name, detail, depends, got, expected in nogo:
            print(f"  x {src}/{fname}: {name}: {detail}")
            if depends:
                print(f"      used by: {depends}")
            # Turn the drift into a copy-pasteable action: show the NEW value and
            # the two edits. We do NOT auto-apply — these are safety-critical
            # (a wrong reset/ROM addr wedges the part), so a human bench-verifies.
            if got is None:
                print("      => symbol VANISHED upstream (renamed/removed) — "
                      "re-audit the port by hand")
            else:
                gv = f"{got} (0x{got:x})" if isinstance(got, int) else repr(got)
                print(f"      => upstream is now {gv} (was {expected!r}). To adopt:")
                print(f"         1. bench-verify the new value is correct for the part")
                print(f"         2. update the espjtag value in 'used by' above")
                print(f"         3. set this symbol's expected={got!r} in upstream.lock")
        print("\n(Then bump pinned_sha in upstream.lock and re-run "
              "scripts/xcheck.py on hardware to confirm the live behaviour.)")
        return EXIT_NOGO

    print(f"RESULT: GO -- all {checked} tracked symbols match current upstream. "
          f"espjtag's ports remain valid.")
    print("(Upstream HEAD may have advanced; nothing WE depend on moved. "
          "Bump pinned_sha in upstream.lock at leisure to refresh the baseline.)")
    return EXIT_GO


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FileNotFoundError as e:
        print(f"check_upstream: cannot read upstream.lock: {e}", file=sys.stderr)
        sys.exit(EXIT_UNKNOWN)
    except json.JSONDecodeError as e:
        print(f"check_upstream: upstream.lock is not valid JSON: {e}",
              file=sys.stderr)
        sys.exit(EXIT_UNKNOWN)
