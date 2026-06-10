#!/usr/bin/env python3
"""xcheck.py — multi-chip THREE-WAY differential read: espjtag vs OpenOCD vs probe-rs.

Have independent JTAG stacks read the SAME silicon and assert they agree — the real
proof a selftest can't give. Chip-profile driven (--chip): RISC-V parts (C6/C5) get
the full three-way diff; Xtensa parts (S3) get what's physically possible.

  RISC-V (esp32c6/esp32c5)  espjtag drives the RISC-V Debug Module directly, so all
      three tools read: IDCODE, DTMCS, DM registers (via OpenOCD `riscv dmi_read`),
      CPU-ID CSRs (brief halt), memory, the GPIO bank, temp sensor + counters.

  Xtensa (esp32s3)  espjtag is RISC-V ONLY (issue #9 — it can read the TAP IDCODE but
      NOT drive the Xtensa OCD module). So espjtag contributes IDCODE only, and the
      memory/GPIO cross-check runs OpenOCD <-> probe-rs (both Xtensa-capable). Xtensa
      has no background System-Bus access, so its memory read needs a brief halt.

Each field is classed invariant (MUST match — same silicon) or dynamic/run-state
(EXPECTED to differ: DM ownership, run-state, PC, live pins, free-running counters).
Discovery via the bench config DB (esp32-zephyr dev.py): pass the usb_path + MAC.

Usage:
  python3 scripts/xcheck.py --usb 1-1.3.3.3 --serial 40:4C:CA:51:68:88 [--chip esp32c6]
                            [--mem-addr 0x40000000] [--mem-bytes 100]
"""
import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usb.util

from espjtag import EspUsbJtag, chips
from espjtag.constants import (
    DMCONTROL, DMSTATUS, HARTINFO, ABSTRACTCS, SBCS,
    COMMAND, DATA0, CMD_ACCESS_REGISTER, AC_AARSIZE32, AC_TRANSFER,
)

OCD_ROOT = ("/home/dan/.espressif/tools/openocd-esp32/"
            "v0.12.0-esp32-20251215/openocd-esp32")

# Per-chip profile: which debug core, OpenOCD board cfg, probe-rs chip name, an
# immutable memory region for the comparison, and the GPIO bank (base + offsets
# DIFFER per chip — esp-idf register/soc/gpio_reg.h, cross-checked on the bench).
PROFILES = {
    "esp32c6": dict(core="riscv", ocd_cfg="board/esp32c6-builtin.cfg",
                    prs_chip="esp32c6", mem_addr=0x40000000,
                    gpio={"GPIO_OUT": 0x60091004, "GPIO_ENABLE": 0x60091020,
                          "GPIO_STRAP": 0x60091038, "GPIO_IN": 0x6009103C,
                          "GPIO_STATUS": 0x60091044},
                    sensor={"tsens": 0x6000E058}),
    "esp32c5": dict(core="riscv", ocd_cfg="board/esp32c5-builtin.cfg",
                    prs_chip="esp32c5", mem_addr=0x40000000,
                    gpio={"GPIO_STRAP": 0x60091000, "GPIO_OUT": 0x60091004,
                          "GPIO_ENABLE": 0x60091034, "GPIO_IN": 0x60091064,
                          "GPIO_STATUS": 0x60091074},
                    sensor={}),     # C5 TSENS base not bench-verified — don't guess
    "esp32s3": dict(core="xtensa", ocd_cfg="board/esp32s3-builtin.cfg",
                    prs_chip="esp32s3", mem_addr=0x40000000,
                    gpio={"GPIO_OUT": 0x60004004, "GPIO_ENABLE": 0x60004020,
                          "GPIO_STRAP": 0x60004038, "GPIO_IN": 0x6000403C,
                          "GPIO_STATUS": 0x60004044},
                    sensor={}),
}

# RISC-V DM registers (DMI address space) — espjtag AND OpenOCD (`riscv dmi_read`).
DM = {"dmcontrol": DMCONTROL, "dmstatus": DMSTATUS, "hartinfo": HARTINFO,
      "abstractcs": ABSTRACTCS, "sbcs": SBCS}
CSR = {"misa": 0x301, "mvendorid": 0xF11, "marchid": 0xF12, "mimpid": 0xF13,
       "mhartid": 0xF14, "dpc": 0x7B1, "dcsr": 0x7B0}
COUNTER = {"mcycle": 0xB00, "minstret": 0xB02}
INVARIANT = {"idcode", "dtmcs", "hartinfo", "abstractcs",
             "misa", "mvendorid", "marchid", "mimpid", "mhartid"}
SBCS_ID_MASK = 0xE0000FFF


def read_csr_checked(j, csr):
    """Abstract-command CSR read that also returns cmderr (abstract access is
    optional per-CSR: the C6 traps HPM counters mcycle/minstret -> stale DATA0)."""
    cmd = CMD_ACCESS_REGISTER | AC_AARSIZE32 | AC_TRANSFER | (csr & 0xFFFF)
    j.dmi_write(COMMAND, cmd)
    err = j._abstract_wait()
    return j.dm_read(DATA0), err


# ---------------------------------------------------------------- espjtag ----
def read_espjtag(usb_path, prof, mem_addr, nwords):
    j = EspUsbJtag(usb_path)
    out = {"idcode": j.read_idcode()}
    if prof["core"] != "riscv":
        # Xtensa: espjtag can read the TAP IDCODE but NOT the debug module (#9).
        usb.util.dispose_resources(j.dev)
        return out
    out["dtmcs"] = j.read_dtmcs()
    j.examine()
    for nm, addr in DM.items():
        out[nm] = j.dm_read(addr)
    for nm, addr in {**prof["gpio"], **prof["sensor"]}.items():
        out[nm] = j.read_mem32(addr)
    out["mem"] = j.read_mem(mem_addr, nwords)
    if j.halt():
        for nm, csr in CSR.items():
            out[nm] = j.read_register(csr)
        for nm, csr in COUNTER.items():
            v, e = read_csr_checked(j, csr)
            out[nm] = v if e == 0 else None
            out[nm + "_err"] = e
        dpc1 = out.get("dpc")
        j.resume()
        if j.halt():                                # liveness: PC must have moved
            dpc2 = j.read_register(CSR["dpc"])
            out["dpc2"] = dpc2
            out["pc_moved"] = dpc1 is not None and dpc2 != dpc1
            v2, e2 = read_csr_checked(j, COUNTER["mcycle"])
            if e2 == 0 and out.get("mcycle") is not None:
                out["mcycle_delta"] = (v2 - out["mcycle"]) & 0xFFFFFFFF
            j.resume()
    else:
        out["_halt_failed"] = True
    usb.util.dispose_resources(j.dev)
    return out


# ---------------------------------------------------------------- OpenOCD ----
def read_openocd(usb_loc, prof, mem_addr, nwords):
    core, gpio, sensor = prof["core"], prof["gpio"], prof["sensor"]
    argv = [f"{OCD_ROOT}/bin/openocd",
            "-s", f"{OCD_ROOT}/share/openocd/scripts",
            "-c", f"adapter usb location {usb_loc}",
            "-f", prof["ocd_cfg"], "-c", "init", "-c", "targets"]
    if core == "riscv":
        argv += ["-c", "riscv set_mem_access sysbus"]
        for nm, addr in DM.items():
            argv += ["-c", f'echo "X {nm}=[format 0x%08x [riscv dmi_read 0x{addr:02x}]]"']
        for nm, addr in {**gpio, **sensor}.items():
            argv += ["-c", f'echo "X {nm}=[format 0x%08x [lindex [read_memory '
                            f'0x{addr:08x} 32 1] 0]]"']
        argv += ["-c", f'echo "X mem=[read_memory 0x{mem_addr:08x} 32 {nwords}]"']
        argv += ["-c", "halt"]
        for nm in list(CSR) + list(COUNTER):
            argv += ["-c", f"reg {nm} -force"]
        argv += ["-c", "resume"]
    else:
        # Xtensa: no background sysbus — halt to read memory, then resume.
        argv += ["-c", "halt"]
        for nm, addr in gpio.items():
            argv += ["-c", f'echo "X {nm}=[format 0x%08x [lindex [read_memory '
                            f'0x{addr:08x} 32 1] 0]]"']
        argv += ["-c", f'echo "X mem=[read_memory 0x{mem_addr:08x} 32 {nwords}]"']
        argv += ["-c", "resume"]
    argv += ["-c", "exit"]
    r = subprocess.run(argv, capture_output=True, text=True)
    text = r.stdout + r.stderr
    out = {}
    m = re.search(r"tap/device found:\s*(0x[0-9a-fA-F]+)", text)
    if m:
        out["idcode"] = int(m.group(1), 16)
    m = re.search(r"found (\d+) harts", text)
    if m:
        out["harts"] = int(m.group(1))
    # `targets` table: each MULTI-CORE part lists its cores + run state. Rows look
    # like "  0* esp32s3.cpu0  esp32s3  little  esp32s3.cpu0  running".
    cores = re.findall(r"^\s*\d+\*?\s+(\S+)\s+\S+\s+\S+\s+\S+\s+(\w+)\s*$", text, re.M)
    if cores:
        out["cores"] = cores
    for m in re.finditer(r"^X (\w+)=(0x[0-9a-fA-F]+)\s*$", text, re.M):
        out[m.group(1)] = int(m.group(2), 16)
    m = re.search(r"^X mem=(.+)$", text, re.M)               # echo->stderr, hex tokens
    if m:
        out["mem"] = [int(x, 16) & 0xFFFFFFFF
                      for x in re.findall(r"0x[0-9a-fA-F]+", m.group(1))]
    for m in re.finditer(r"^(\w+) \(/\d+\): (0x[0-9a-fA-F]+)", text, re.M):
        if m.group(1) in CSR or m.group(1) in COUNTER:
            out[m.group(1)] = int(m.group(2), 16)
    out["_raw"] = text
    return out


# ---------------------------------------------------------------- probe-rs ----
def _probers(serial, chip, args):
    return subprocess.run(["probe-rs", *args, "--chip", chip,
                           "--probe", f"303a:1001:{serial}"],
                          capture_output=True, text=True)


def read_probers(serial, prof, mem_addr, nwords):
    chip = prof["prs_chip"]
    out = {}
    r = _probers(serial, chip, ["info"])
    # probe-rs `info` prints BOTH a RISC-V and an Xtensa "IDCODE:" line; the absent
    # core reads all-zero (e.g. 0000000000 on an S3's RISC-V probe). Take the first
    # NON-zero one — the real part, whichever core it is.
    nz = [int(x, 16) for x in re.findall(r"IDCODE:\s*([0-9a-fA-F]+)", r.stdout)
          if int(x, 16) != 0]
    if nz:
        out["idcode"] = nz[0] & 0xFFFFFFFF
    r = _probers(serial, chip, ["read", "b32", hex(mem_addr), str(nwords)])
    toks = re.findall(r"[0-9a-fA-F]{8}", r.stdout)
    if toks:
        out["mem"] = [int(x, 16) for x in toks]
    for nm, addr in {**prof["gpio"], **prof["sensor"]}.items():
        r = _probers(serial, chip, ["read", "b32", hex(addr), "1"])
        t = re.findall(r"[0-9a-fA-F]{8}", r.stdout)
        if t:
            out[nm] = int(t[0], 16)
    return out


# ---------------------------------------------------------------- report ----
def h(v):
    return "n/a" if v is None else f"0x{v & 0xFFFFFFFF:08x}"


def diff_row(name, vals):
    present = [v for v in vals if v is not None]
    if len(present) <= 1:
        verdict = "(single)"
    elif all(v == present[0] for v in present):
        verdict = "MATCH"
    else:
        verdict = "DIFF ✗" if name in INVARIANT else "diff (expected)"
    tag = "  " if name in INVARIANT else " ~"
    ok = verdict != "DIFF ✗"
    return (f"  {tag}{name:12} esp={h(vals[0])}  ocd={h(vals[1])}  "
            f"prs={h(vals[2])}   {verdict}"), ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb", required=True)
    ap.add_argument("--serial", required=True)
    ap.add_argument("--chip", default="esp32c6", choices=list(PROFILES))
    ap.add_argument("--mem-addr", default=None)
    ap.add_argument("--mem-bytes", type=int, default=100)
    args = ap.parse_args()
    prof = PROFILES[args.chip]
    mem_addr = int(args.mem_addr, 0) if args.mem_addr else prof["mem_addr"]
    nwords = (args.mem_bytes + 3) // 4

    print(f"three-way differential read  chip={args.chip} core={prof['core']}  "
          f"usb={args.usb}  serial={args.serial}")
    print(f"mem: {nwords*4} bytes from 0x{mem_addr:08x}\n")

    esp = read_espjtag(args.usb, prof, mem_addr, nwords)
    ocd = read_openocd(args.usb, prof, mem_addr, nwords)
    prs = read_probers(args.serial, prof, mem_addr, nwords)

    all_ok = True

    def section(title, names):
        nonlocal all_ok
        print(f"-- {title} --")
        for nm in names:
            line, ok = diff_row(nm, [esp.get(nm), ocd.get(nm), prs.get(nm)])
            print(line)
            all_ok = all_ok and ok
        print()

    section("identity (invariant)", ["idcode", "dtmcs"])

    # Dual-core awareness: list EVERY core OpenOCD examined + its run state. C6 = HP
    # + LP RISC-V (2 harts); S3 = PRO + APP Xtensa (2 targets); C5's cfg exposes 1.
    cores = ocd.get("cores") or []
    harts = ocd.get("harts") or 0
    print("-- cores (OpenOCD targets — these are multi-core parts) --")
    for nm, st in cores:
        print(f"    {nm:22} {st}")
    if len(cores) <= 1 and harts > 1:                   # C6: 2 harts under 1 target
        print(f"    (1 OpenOCD target exposing {harts} harts — e.g. C6 HP + LP)")
    if prof["core"] == "riscv":
        print(f"    espjtag drives the primary hart (mhartid={h(esp.get('mhartid'))}); "
              f"memory below is the shared bus — identical for every core")
    print(f"  CORES targets={len(cores)} harts={harts}")
    print()

    if prof["core"] == "riscv":
        section("DM registers (dmcontrol/dmstatus = ownership+run-state, may differ)",
                list(DM))
        se, so = esp.get("sbcs"), ocd.get("sbcs")
        if se is not None and so is not None:
            ok = (se & SBCS_ID_MASK) == (so & SBCS_ID_MASK)
            print(f"  sbcs identity subfields (mask 0x{SBCS_ID_MASK:08x}): "
                  f"esp=0x{se & SBCS_ID_MASK:08x} ocd=0x{so & SBCS_ID_MASK:08x}  "
                  f"{'MATCH' if ok else 'DIFF ✗'}\n")
            all_ok = all_ok and ok
        section("CPU-ID CSRs (invariant: misa/vendor/arch/imp/hart)", list(CSR))
        section("sensors & counters (live / free-running — expected to differ)",
                list(prof["sensor"]) + list(COUNTER))
        for nm in COUNTER:                          # honest: may be unsupported
            if esp.get(nm) is None:
                print(f"   ~{nm:12} esp=UNSUPPORTED(cmderr={esp.get(nm+'_err')})  "
                      f"ocd={h(ocd.get(nm))}  -> no abstract-cmd access (progbuf only)")
        ts = esp.get("tsens")
        if ts is not None:
            raw = ts & 0xFF
            note = ("sensor NOT converting (app didn't power it)" if raw in (0, 128)
                    else "live code; needs cal constants for °C")
            print(f"  temp: raw TSENS_OUT={raw} -> {note}")
        if esp.get("pc_moved") is not None:
            v = "LIVE (PC advanced)" if esp["pc_moved"] else "did NOT advance"
            print(f"  liveness: dpc {h(esp.get('dpc'))} -> {h(esp.get('dpc2'))} "
                  f"-> core is {v}")
        print()
    else:
        print("-- NOTE: Xtensa part — espjtag is RISC-V only (issue #9), so it "
              "contributes\n   IDCODE only; memory/GPIO below is an OpenOCD <-> "
              "probe-rs cross-check.\n")

    # GPIO bank — all 3 on RISC-V; OpenOCD<->probe-rs (esp=n/a) on Xtensa.
    section("GPIO bank (live pin state — expected to differ)", list(prof["gpio"]))

    # memory block — element-wise across the present tools.
    print(f"-- memory: {nwords} words @ 0x{mem_addr:08x} (invariant if ROM) --")
    em, om, pm = esp.get("mem"), ocd.get("mem"), prs.get("mem")
    mem_ok, shown = True, 0
    for i in range(nwords):
        e = em[i] if em and i < len(em) else None
        o = om[i] if om and i < len(om) else None
        p = pm[i] if pm and i < len(pm) else None
        present = [v for v in (e, o, p) if v is not None]
        ok = len(present) <= 1 or all(v == present[0] for v in present)
        mem_ok = mem_ok and ok
        if not ok or shown < 4:
            print(f"     [{i:2}] @0x{mem_addr+i*4:08x}  esp={h(e)} ocd={h(o)} "
                  f"prs={h(p)}   {'MATCH' if ok else 'DIFF'}")
            shown += 1
    ntools = sum(1 for x in (em, om, pm) if x)
    print(f"  -> {nwords}/{nwords} words agree across {ntools} tool(s)" if mem_ok
          else "  -> MEMORY MISMATCH")
    all_ok = all_ok and mem_ok
    print()

    gin = esp.get("GPIO_IN")
    gin = gin if gin is not None else ocd.get("GPIO_IN")
    if gin is not None:
        hi = [f"GPIO{p}" for p in range(32) if gin & (1 << p)]
        print(f"-- pins high (GPIO_IN=0x{gin:08x}) --\n  "
              + (" ".join(hi) if hi else "(none high)") + "\n")

    print("=" * 64)
    print("RESULT:", "ALL INVARIANTS AGREE ✓" if all_ok
          else "INVARIANT MISMATCH ✗ — investigate")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
