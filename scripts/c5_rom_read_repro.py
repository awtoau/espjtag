#!/usr/bin/env python3
"""c5_rom_read_repro.py — reproduce + bisect #33: C5 ROM legacy flash read returns
deterministic garbage when attaching to a long-running app.

Soak signature (3 independent hits): the brick-safety gate (_rom_flash_ready) fails
ONLY in the slot that connects right after a flash_incremental + resume cycle on a
running app; flash@0 reads the SAME wrong words every time; flash_init
(attach + config_param) does NOT clear it within the connection, but the next
halt/resume cycle always does.

Hypothesis: the app leaves SPI1 in a fast/quad read mode; config_param restores
geometry only — readmode/clk stay wrong. The C5 ROM exports
esp_rom_spiflash_config_readmode (0x4000018C) / config_clk (0x40000188), now tabled.

This script loops the exact failing rhythm. On each gate failure it tries REMEDIES
in order, re-testing the gate after each, and reports which one recovers:
  r1: flash_init again            (attach + config_param — known-insufficient)
  r2: config_readmode(SLOWRD=5)   (the hypothesis)
  r3: config_clk(div=1) + r1      (clock reconfig)
  r4: resume + re-halt bounce     (known-good from soak — the control)

Usage: python3 scripts/c5_rom_read_repro.py [--usb 1-1.2] [--cycles 12]
"""
import argparse
import random
import sys
import time

sys.path.insert(0, ".")
import usb.util
from espjtag import EspUsbJtag

SEC = 0x1000
ADDR = 0x300000
SLOWRD = 5                       # ESP_ROM_SPIFLASH_SLOWRD_MODE (esp_rom_spiflash.h)


def gate(j):
    ready, words, magic = j._rom_flash_ready()
    return ready, words


def remedies(j):
    """On a failed gate, try each remedy and return the first that recovers."""
    def r1():
        j.flash_init()

    def r2():
        j.call_rom("spiflash_config_readmode", args=(SLOWRD,))

    def r3():
        j.call_rom("spiflash_config_clk", args=(1, 1))   # div=1 on SPI1
        j.flash_init()

    def r4():
        j.resume()
        if not j.halt():
            raise RuntimeError("re-halt failed")

    for name, fn in (("r1 flash_init", r1), ("r2 readmode(SLOWRD)", r2),
                     ("r3 config_clk+init", r3), ("r4 resume/halt bounce", r4)):
        try:
            fn()
        except Exception as e:                              # noqa: BLE001
            print(f"      {name}: raised {e}")
            continue
        ok, words = gate(j)
        print(f"      {name}: gate {'RECOVERED' if ok else 'still bad'}"
              f"{'' if ok else '  words=' + ','.join(f'{w:08x}' for w in (words or []))}")
        if ok:
            return name
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb", default="1-1.2")
    ap.add_argument("--cycles", type=int, default=12)
    args = ap.parse_args()

    rnd = random.Random(0x33)
    A = bytes(rnd.randbytes(16 * SEC))
    fails, recoveries = 0, {}
    for cyc in range(args.cycles):
        j = EspUsbJtag(args.usb)
        try:
            j.examine()
            if not j.halt():
                print(f"cycle {cyc}: halt failed"); continue
            ok, words = gate(j)
            if not ok:
                fails += 1
                print(f"cycle {cyc}: GATE FAIL  words="
                      + ",".join(f"{w:08x}" for w in (words or [])))
                won = remedies(j)
                recoveries[won] = recoveries.get(won, 0) + 1
                if won is None:
                    print("      NO remedy recovered — stopping for inspection")
                    return 2
            else:
                print(f"cycle {cyc}: gate ok")
            # the failing rhythm: incremental write + resume, reconnect next cycle
            B = bytearray(A)
            B[(cyc % 16) * SEC + 0x20] ^= 0xFF
            if not gate(j)[0]:
                j.flash_init()
            j.flash_incremental(ADDR, bytes(B), verify=False)
        finally:
            try:
                j.resume()
            finally:
                usb.util.dispose_resources(j.dev)
        # the soak's failing slot hit ~9 s after app boot; the app needs to RUN
        # between cycles to wedge SPI1 — wait for its boot/flash-IO window by
        # polling cycles of the LIVE app rather than a blind sleep: reconnect
        # happens immediately; the failure (if armed) shows at the next gate.
    print(f"\n{args.cycles} cycles: {fails} gate failures; recoveries: {recoveries}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
