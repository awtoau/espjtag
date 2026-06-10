#!/usr/bin/env python3
"""cpu_load.py — estimate CPU load over JTAG by PC sampling (statistical profiler).

There is NO hardware "CPU load" register on the ESP32 RISC-V (C6/C5) or Xtensa (S3)
parts — load is an OS concept. But it's derivable: halt -> read PC -> resume, many
times; the fraction of samples NOT parked at the idle/WFI loop ≈ CPU load. That's
exactly how sample profilers work, and it's chip-agnostic (OpenOCD logs the PC on
every halt). The trade-off: each halt perturbs the target slightly, this is a
STATISTICAL estimate, and it's PER-CORE — these are dual-core parts (C6 HP+LP,
S3 PRO+APP), and OpenOCD tags each halt line with the core, so we split by core.
Mixing cores into one stream gives a meaningless number.

CAVEAT on "idle": we call the densest PC cluster the parked spot. A core parked
in WFI is genuinely idle; but a core stuck in a TIGHT busy spin-loop also parks at
one PC and would read as "idle" here — distinguishing the two needs the firmware's
idle-thread symbol. So this measures "parked vs spread", a good load proxy, not a
guaranteed idle/active truth.

Discovery via the bench config DB (esp32-zephyr dev.py) — pass the usb_path.

Usage:  python3 scripts/cpu_load.py --usb 1-1.3.3.3 --chip esp32c6 [--samples 60]
"""
import argparse
import collections
import os
import re
import subprocess

OCD_ROOT = ("/home/dan/.espressif/tools/openocd-esp32/"
            "v0.12.0-esp32-20251215/openocd-esp32")
CFG = {"esp32c6": "board/esp32c6-builtin.cfg",
       "esp32c5": "board/esp32c5-builtin.cfg",
       "esp32s3": "board/esp32s3-builtin.cfg"}
IDLE_WINDOW = 0x80          # PCs within this span of the mode = the idle/WFI loop


def sample_pcs(usb_loc, chip, n):
    """Halt/read-PC/resume n times via OpenOCD; return the list of sampled PCs.
    Parsed from OpenOCD's "Target halted, PC=0x.." log line (printed every halt)."""
    loop = "for {set i 0} {$i < %d} {incr i} { halt; resume }" % n
    argv = [f"{OCD_ROOT}/bin/openocd",
            "-s", f"{OCD_ROOT}/share/openocd/scripts",
            "-c", f"adapter usb location {usb_loc}",
            "-f", CFG[chip], "-c", "init", "-c", loop, "-c", "exit"]
    r = subprocess.run(argv, capture_output=True, text=True)
    text = r.stdout + r.stderr
    # OpenOCD tags each halt with the core: "[esp32s3.cpu0] Target halted, PC=0x.."
    return [(core, int(pc, 16)) for core, pc in
            re.findall(r"\[([^\]]+)\] Target halted, PC=0x([0-9a-fA-F]+)", text)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb", required=True)
    ap.add_argument("--chip", required=True, choices=list(CFG))
    ap.add_argument("--samples", type=int, default=60)
    args = ap.parse_args()

    samples = sample_pcs(args.usb, args.chip, args.samples)
    if not samples:
        print("no PC samples captured — OpenOCD couldn't halt the core")
        return 1

    bycore = collections.defaultdict(list)
    for core, pc in samples:
        bycore[core].append(pc)
    print(f"chip={args.chip} usb={args.usb}  {len(samples)} samples over "
          f"{len(bycore)} core(s) — PER-CORE (these are dual-core parts)\n")

    for core in sorted(bycore):
        pcs = bycore[core]
        hist = collections.Counter(pcs)
        mode_pc, _ = hist.most_common(1)[0]
        parked = sum(c for pc, c in hist.items() if abs(pc - mode_pc) <= IDLE_WINDOW)
        off = len(pcs) - parked
        load = 100.0 * off / len(pcs)
        state = (f"PARKED at 0x{mode_pc:08x} — idle or a tight spin (needs the idle "
                 f"symbol to tell which)" if load < 15 else "ACTIVE — PC spread across code")
        print(f"core {core}:  {len(pcs)} samples")
        print(f"   parked 0x{mode_pc:08x} ±0x{IDLE_WINDOW:x}: {parked} "
              f"({100.0*parked/len(pcs):.0f}%)   off-park: {off} ({load:.0f}%)")
        print(f"   -> load proxy ~= {load:.0f}%   [{state}]")
        for pc, c in hist.most_common(4):
            tag = "park" if abs(pc - mode_pc) <= IDLE_WINDOW else "OFF "
            print(f"        0x{pc:08x} x{c:<3} {tag}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
