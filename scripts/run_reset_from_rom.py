#!/usr/bin/env python3
"""Call espjtag.EspUsbJtag.reset_run_from_rom() on xiao-c6-b with verbose log.
Single invocation — the harness (test_rom_3x.py) drives the 3x flash+boot loop."""
import sys

import usb.util

sys.path.insert(0, "/home/dan/git/espjtag")
from espjtag import EspUsbJtag                       # noqa: E402

USB_PATH = "1-1.3.1.3.4"


def main():
    j = EspUsbJtag(USB_PATH)
    ran = j.reset_run_from_rom(log=print)
    try:
        usb.util.dispose_resources(j.dev)
    except Exception:                                # noqa: BLE001
        pass
    print(f"reset_run_from_rom -> {ran}")
    return 0 if ran else 1


if __name__ == "__main__":
    sys.exit(main())
