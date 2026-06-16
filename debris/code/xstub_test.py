#!/usr/bin/env python3
"""xstub_test.py — #29 hardware bring-up: load + run an OpenOCD S3 flasher stub
over espjtag, watch if it halts at the exit BREAK (the windowed-exec blocker).

Step-wise so a failure is localised:
  1. connect + XtensaXDM + powerup + halt
  2. load cmd_test1 (the simplest stub) into RAM; read back to confirm it landed
  3. run it; report halted? + the return code (a2)

Usage: python3 scripts/xstub_test.py [usb_path] [--cmd cmd_test1]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from espjtag import EspUsbJtag                                  # noqa: E402
from espjtag.xtensa import XtensaXDM                            # noqa: E402
from espjtag.xtensa_flasher import XtensaFlasher                # noqa: E402
from espjtag.xtensa_stubs import STUBS                          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("usb", nargs="?", default="1-1.3.3.4")
    ap.add_argument("--serial")
    ap.add_argument("--cmd", default="cmd_test1")
    args = ap.parse_args()

    j = EspUsbJtag(serial=args.serial) if args.serial else EspUsbJtag(args.usb)
    x = XtensaXDM(j)
    print(f"[1] connected; chip={x._chip_dict.get('name','?')}")
    x.powerup()
    ok = x.halt()
    print(f"    powerup + halt -> halted={ok}")
    if not ok:
        print("    HALT FAILED — cannot proceed"); return 1

    fl = XtensaFlasher(x, chip="esp32s3")
    cfg = STUBS["esp32s3"][args.cmd]
    print(f"[2] loading {args.cmd}: code={len(cfg['code'])}B@0x{cfg['iram_org']:x} "
          f"data={len(cfg['data'])}B@0x{cfg['dram_org']:x} entry=0x{cfg['entry_addr']:x}")
    st = fl.load(args.cmd)
    # read back the first 8 words of code to confirm it landed
    back = x.read_mem(cfg["iram_org"], 8)
    exp = [int.from_bytes(cfg["code"][i:i + 4], "little") for i in range(0, 32, 4)]
    match = back == exp
    print(f"    code readback @0x{cfg['iram_org']:x}: "
          f"{'MATCH' if match else 'MISMATCH'}")
    print(f"      wrote: {[hex(w) for w in exp]}")
    print(f"      read : {[hex(w) for w in back]}")
    print(f"    tramp_mapped=0x{st['tramp_mapped_addr']:x} entry=0x{st['entry']:x} "
          f"sp=0x{st['stack_addr']:x} trap_entry=0x{st['trap_entry_addr']:x}")

    print(f"[3] running {args.cmd} (no args)...")
    try:
        rc = fl.run(args=(), timeout_ms=2000)
        print(f"    *** HALTED at BREAK, return code (a2) = 0x{rc:x} ***")
    except RuntimeError as e:
        print(f"    FAILED: {e}")
        # diag: where is the PC now?
        try:
            from espjtag.xtensa import INS_RSR_EPC6_A3
            pc = x._get_sr_a3(INS_RSR_EPC6_A3)
            print(f"    EPC6 (PC) now = 0x{pc:x}")
        except Exception as e2:
            print(f"    (couldn't read PC: {e2})")
        return 1
    finally:
        x.resume()
    return 0


if __name__ == "__main__":
    sys.exit(main())
