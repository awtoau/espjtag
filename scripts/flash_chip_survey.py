#!/usr/bin/env python3
"""flash_chip_survey.py — per-board flash die identification + measured timings.

The fleet benches exposed up to ~25% flash-op spread between "identical" boards;
flash-id showed three different flash manufacturers. This survey makes that a
reported, recorded fact instead of a surprise:

  per RISC-V fleet board:
    - JEDEC ID via esptool flash-id  (manufacturer / device / size)
    - SFDP header presence via esptool read-flash-sfdp (the standard way a host
      discovers a die's geometry + timings)
    - MEASURED per-sector timings via espjtag ROM calls at the 0x300000 test
      window: erase ms, 4 KiB program ms, 4 KiB ROM-read ms (medians of N)

Usage: python3 scripts/flash_chip_survey.py [--sectors 4]
Output: table on stdout (tee to tmp/ by the caller).
"""
import argparse
import random
import re
import statistics
import subprocess
import sys
import time

sys.path.insert(0, ".")
import usb.util
from espjtag import EspUsbJtag

DEVPY = "/home/dan/git/esp32-zephyr/scripts/dev.py"
SEC = 0x1000
ADDR = 0x300000
VENDORS = {0x20: "XMC", 0xEF: "Winbond", 0xC8: "GigaDevice", 0x85: "Puya",
           0x68: "Boya", 0xA1: "Fudan", 0x0B: "XTX", 0x46: "(unidentified 0x46)"}


def fleet():
    out = subprocess.run(["python3", DEVPY, "fleet-status"],
                         capture_output=True, text=True).stdout
    boards = []
    for line in out.splitlines():
        if "●" not in line:
            continue
        m = re.search(r"(\S+)\s+esp32(c[56])\s+(\d[\d.-]+)", line)
        t = re.search(r"/dev/ttyACM\d+", line)
        if m and t:
            boards.append((m.group(1), f"esp32{m.group(2)}", m.group(3), t.group(0)))
    return boards


def esptool_id(chip, tty):
    r = subprocess.run(["esptool", "--chip", chip, "--port", tty, "flash-id"],
                       capture_output=True, text=True)
    mfg = re.search(r"Manufacturer:\s*([0-9a-fA-F]+)", r.stdout)
    dev = re.search(r"Device:\s*([0-9a-fA-F]+)", r.stdout)
    size = re.search(r"flash size.*?:\s*(\S+)", r.stdout)
    sf = subprocess.run(["esptool", "--chip", chip, "--port", tty,
                         "read-flash-sfdp", "0", "4"], capture_output=True, text=True)
    sfdp = "yes" if "53464450" in sf.stdout.replace(" ", "") or "SFDP" in sf.stdout \
        else ("yes" if re.search(r"50444653|53464450", sf.stdout) else "?")
    return (int(mfg.group(1), 16) if mfg else None,
            int(dev.group(1), 16) if dev else None,
            size.group(1) if size else "?", sfdp)


def jtag_timings(usb_path, nsec):
    j = EspUsbJtag(usb_path)
    j.examine()
    if not j.halt():
        raise RuntimeError("halt failed")
    try:
        if not j._rom_flash_ready()[0]:
            j.flash_init()
        buf = j._chip()["sram"]["data"]
        rnd = random.Random(0x51)
        er, wr, rd = [], [], []
        for s in range(nsec):
            sa = ADDR + s * SEC
            img = rnd.randbytes(SEC)
            j.call_rom("spiflash_unlock")
            t0 = time.perf_counter()
            r, _ = j.call_rom("spiflash_erase_sector", args=(sa >> 12,))
            er.append((time.perf_counter() - t0) * 1000)
            if r:
                raise RuntimeError(f"erase -> {r}")
            j.write_mem(buf, [int.from_bytes(img[i:i + 4], "little")
                              for i in range(0, SEC, 4)])
            t0 = time.perf_counter()
            r, _ = j.call_rom("spiflash_write", args=(sa, buf, SEC))
            wr.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            j.call_rom("spiflash_read", args=(sa, buf, SEC))
            rd.append((time.perf_counter() - t0) * 1000)
        med = lambda v: statistics.median(v)
        return med(er), med(wr), med(rd)
    finally:
        try:
            j.resume()
        finally:
            usb.util.dispose_resources(j.dev)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sectors", type=int, default=4)
    args = ap.parse_args()
    boards = fleet()
    print(f"{'board':12s} {'chip':8s} {'flash die':26s} {'size':5s} {'SFDP':4s} "
          f"{'erase':>9s} {'prog4K':>9s} {'read4K':>9s}")
    for name, chip, usb_path, tty in boards:
        mfg, dev, size, sfdp = esptool_id(chip, tty)
        vendor = VENDORS.get(mfg, f"0x{mfg:02x}" if mfg is not None else "?")
        die = f"{vendor} {mfg:02x}:{dev:04x}" if mfg is not None else "?"
        try:
            er, wr, rd = jtag_timings(usb_path, args.sectors)
            t = f"{er:7.1f}ms {wr:7.1f}ms {rd:7.1f}ms"
        except Exception as e:                                  # noqa: BLE001
            t = f"timing failed: {e}"
        print(f"{name:12s} {chip:8s} {die:26s} {size:5s} {sfdp:4s} {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
