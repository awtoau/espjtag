"""CLI: `python -m espjtag [usb_path] [--serial MAC]
            [--selftest | --info | --reset-run | --reset-from-rom]`

Pin a unit by positional usb_path OR `--serial MAC` (the USB serial; STABLE
across re-enumeration, colon/case tolerant — preferred when several ESPs are on
the bus). Reset subcommands boot the chip over JTAG with NO USB re-enumeration
(see docs/RESET-WITHOUT-REENUMERATION.md):
  --reset-run        ndmreset -> boot the app (the normal reboot)
  --reset-from-rom   USB-bus reset + ndmreset -> boot OUT of post-flash ROM
                     download mode (when a plain reset lands back in ROM)
"""
import sys

import usb.util

from . import chips
from .debug import EspUsbJtag, selftest


def _opt(name):
    """Value of `--name VALUE` from argv, or None."""
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


USAGE = """\
espjtag — pure-Python ESP32 USB-Serial/JTAG debugger

Usage:
  python -m espjtag [USB_PATH] [--serial MAC] [ACTION]

Pin a unit (when several ESPs are on the bus):
  USB_PATH            bus-port path, e.g. 1-1.3.1.3.4 (volatile)
  --serial MAC        USB serial, e.g. 58:E6:C5:11:B7:EC — STABLE across
                      re-enumeration, colon/case tolerant (PREFERRED)

Actions (default = print transport + Debug Module diag):
  --info              chip identity + flash info (JTAG, no stub, no serial)
  --selftest          run the built-in self-test
  --reset-run         ndmreset -> boot the app (normal reboot; NO USB re-enum)
  --reset-from-rom    USB-bus reset + ndmreset -> boot OUT of post-flash ROM
                      download mode (when a plain reset lands back in ROM)
  -h, --help          this help

Reset actions boot the chip over JTAG without dropping USB (no re-enumeration,
no buttons) — see docs/RESET-WITHOUT-REENUMERATION.md.

Examples:
  python -m espjtag --info --serial 58:E6:C5:11:B7:EC
  python -m espjtag --reset-from-rom --serial 58:E6:C5:11:B7:EC
"""


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(USAGE)
        return 0
    serial = _opt("--serial")
    # positional usb_path = first bare arg that isn't a flag or a flag's value
    flag_vals = {v for f in ("--serial",) for v in [_opt(f)] if v}
    args = [a for a in sys.argv[1:]
            if not a.startswith("--") and a not in flag_vals]
    path = args[0] if args else None

    if "--reset-run" in sys.argv:
        from .reset import reset_run
        reset_run(usb_path=path, serial=serial)
        print(f"reset-run: booted via ndmreset over JTAG (no USB re-enumeration)"
              f"{' serial=' + serial if serial else ''}")
        return 0
    if "--reset-from-rom" in sys.argv:
        j = EspUsbJtag(path, serial=serial) if serial else EspUsbJtag(path)
        j.examine()
        j.reset_run_from_rom(log=print)
        print("reset-from-rom: USB-bus reset + ndmreset — app should be booting.")
        return 0
    if "--selftest" in sys.argv:
        ok, total = selftest(path)
        return 0 if ok == total else 1
    if "--info" in sys.argv:
        j = EspUsbJtag(path, serial=serial) if serial else EspUsbJtag(path)
        try:
            j.examine()
            ic = j.read_idcode()
            name = chips.name_for(ic) or "?"
            print(f"chip:  {name}  (IDCODE 0x{ic:08x})")
            if not j.halt():
                print("flash: (halt failed — cannot query)"); return 1
            try:
                fi = j.flash_info()
                print(f"flash: {fi['vendor']} {fi['mfg']:02x}:{fi['device']:04x} "
                      f"{fi['size_mb']} MB  (JEDEC RDID 0x{fi['rdid']:06x}, read "
                      "over JTAG via SPI1 — no stub, no serial)")
            finally:
                j.resume()
        finally:
            usb.util.dispose_resources(j.dev)
        return 0
    j = EspUsbJtag(path, serial=serial) if serial else EspUsbJtag(path)
    print(f"espjtag: JTAG iface {j.iface}, "
          f"ep_out 0x{j.ep_out.bEndpointAddress:02x} "
          f"ep_in 0x{j.ep_in.bEndpointAddress:02x}")
    j.diag(log=print)
    return 0


if __name__ == "__main__":
    sys.exit(main())
