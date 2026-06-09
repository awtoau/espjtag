"""CLI: `python -m espjtag [usb_path] [--selftest]`."""
import sys

from .jtag import EspUsbJtag, selftest


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else None
    if "--selftest" in sys.argv:
        ok, total = selftest(path)
        return 0 if ok == total else 1
    j = EspUsbJtag(path)
    print(f"espjtag: JTAG iface {j.iface}, "
          f"ep_out 0x{j.ep_out.bEndpointAddress:02x} "
          f"ep_in 0x{j.ep_in.bEndpointAddress:02x}")
    j.diag(log=print)
    return 0


if __name__ == "__main__":
    sys.exit(main())
