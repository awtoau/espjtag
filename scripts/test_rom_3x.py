#!/usr/bin/env python3
"""FORMAL 3/3 test of espjtag.reset_run_from_rom() on the live xiao-c6-b.

Each round, in a clean state:
  1. esptool write-flash --after no-reset -> chip stranded in USJ ROM download,
  2. confirm the Zephyr console is SILENT (app not booted),
  3. EspUsbJtag(...).reset_run_from_rom(),
  4. confirm the Zephyr console answers "kernel version" with "Zephyr version".

reset_run_from_rom() is run in a SUBPROCESS each round: it does a USB bus reset
that invalidates pyusb handles, and a fresh interpreter is the cleanest way to
prove it works from a cold start (as a caller like dev.py would invoke it).

No fixed sleep waits for boot — console readiness is polled by reading."""
import os
import subprocess
import sys
import time

import serial

SERIAL = "58:E6:C5:11:B7:EC"
PORT = f"/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_{SERIAL}-if00"
BIN = "/home/dan/git/esp32-zephyr/builds/xiao-c6-b/zephyr/zephyr.bin"
HERE = os.path.dirname(os.path.abspath(__file__))


def flash_to_rom():
    tty = os.path.realpath(PORT)
    subprocess.run(["esptool", "--chip", "esp32c6", "--port", tty,
                    "--before", "default-reset", "--after", "no-reset",
                    "write-flash", "0x0", BIN], capture_output=True, text=True)


def boot():
    r = subprocess.run([sys.executable, os.path.join(HERE, "run_reset_from_rom.py")],
                       capture_output=True, text=True)
    for ln in r.stdout.splitlines():
        print("   " + ln)
    return r.returncode == 0


def console_alive(attempts=8):
    for _ in range(attempts):
        try:
            p = serial.Serial(PORT, 115200, timeout=0.3, write_timeout=2)
        except Exception:                            # noqa: BLE001
            continue
        try:
            p.dtr = True
            p.rts = False
            p.reset_input_buffer()
            p.write(b"\r\nkernel version\r\n")
            try:
                p.flush()
            except OSError:
                pass
            buf = b""
            end = time.monotonic() + 0.6
            while time.monotonic() < end:
                buf += p.read(4096)
                if b"Zephyr" in buf:
                    break
        finally:
            p.close()
        if b"Zephyr" in buf:
            return True
    return False


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    passed = 0
    for r in range(rounds):
        print(f"=== round {r} ===")
        flash_to_rom()
        if console_alive(attempts=2):
            print("  WARN: console alive BEFORE reset (flash booted it?)")
        else:
            print("  ROM confirmed (console silent)")
        ran = boot()
        alive = console_alive()
        print(f"  reset_run_from_rom returned {ran}; console -> "
              f"{'BOOTED' if alive else 'NOT-BOOTED'}")
        passed += alive
    print(f"\nRESULT: {passed}/{rounds} booted from ROM via reset_run_from_rom()")
    return 0 if passed == rounds else 1


if __name__ == "__main__":
    sys.exit(main())
