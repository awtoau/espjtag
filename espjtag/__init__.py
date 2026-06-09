"""espjtag — pure-Python RISC-V JTAG debugger for the ESP32 built-in USB-JTAG.

No OpenOCD binary. Talks the Espressif esp_usb_jtag USB protocol directly and
drives the RISC-V Debug Module: halt/resume, read/write GPRs & CSRs, read/write
memory via System Bus Access, and ndmreset.

    from espjtag import EspUsbJtag
    j = EspUsbJtag()             # first 303a:1001 on the bus (or pass a usb_path)
    print(hex(j.read_idcode()))  # 0x0000dc25 on a C6
    j.examine(); j.halt()
    print(hex(j.read_register(0x1000 + 1)))   # x1 / ra
    print(hex(j.read_mem32(0x42000000)))      # memory-mapped flash
    j.resume()
"""
from .jtag import EspUsbJtag, reset_run, diag, selftest

__all__ = ["EspUsbJtag", "reset_run", "diag", "selftest"]
__version__ = "0.1.0"
