#!/usr/bin/env python3
"""Check whether xiao-c6-b booted its Zephyr app by reading the USB-CDC console.

It re-opens the console a few times because, after a JTAG CPU reset (no USB
re-enumeration), the app re-initialises the CDC-ACM endpoint and the host's
first open may race that. We assert DTR (the C5/C6 console needs it), send
"kernel version", and look for the Zephyr banner. Prints BOOTED / NOT-BOOTED.

Why the retry count (6) and the per-read window (0.6 s): the Zephyr USB-CDC
stack re-enumerates its endpoint within a few hundred ms of app start; 6 opens
covers that with margin, and 0.6 s is comfortably longer than the shell's
echo+reply latency over USB-CDC (measured <100 ms). No fixed sleep is used to
"wait for boot" — we poll by reading, which returns as soon as data arrives.
"""
import sys
import time

import serial

PORT = "/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_58:E6:C5:11:B7:EC-if00"


def try_console(attempts=6):
    for i in range(attempts):
        try:
            p = serial.Serial(PORT, 115200, timeout=0.3, write_timeout=2)
        except Exception as e:                      # noqa: BLE001
            print(f"  attempt {i}: open failed: {e}")
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
        snippet = buf.decode("utf-8", "replace").replace("\r", " ").replace("\n", " ")
        print(f"  attempt {i}: {snippet[:120]!r}")
        if b"Zephyr" in buf:
            return True
    return False


if __name__ == "__main__":
    ok = try_console()
    print("BOOTED" if ok else "NOT-BOOTED")
    sys.exit(0 if ok else 1)
