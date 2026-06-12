"""CLI: `python -m espjtag [usb_path] [--selftest | --info]`."""
import sys

import usb.util

from . import chips
from .debug import EspUsbJtag, selftest


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else None
    if "--selftest" in sys.argv:
        ok, total = selftest(path)
        return 0 if ok == total else 1
    if "--info" in sys.argv:
        j = EspUsbJtag(path)
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
    j = EspUsbJtag(path)
    print(f"espjtag: JTAG iface {j.iface}, "
          f"ep_out 0x{j.ep_out.bEndpointAddress:02x} "
          f"ep_in 0x{j.ep_in.bEndpointAddress:02x}")
    j.diag(log=print)
    return 0


if __name__ == "__main__":
    sys.exit(main())
