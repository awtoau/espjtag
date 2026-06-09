"""espjtag — pure-Python RISC-V JTAG debugger for the ESP32 built-in USB-JTAG.

No OpenOCD binary. Talks the Espressif esp_usb_jtag USB protocol directly and
drives the RISC-V Debug Module.

Layers (see issue #5 — kept separable so a minimal esptool PR can take just the
transport + reset, not the whole debugger):
  espjtag.transport  — esp_usb_jtag USB + JTAG TAP + DMI read/write  (the base)
  espjtag.reset      — the reset-after-flash fix (ndmreset), transport-only
  espjtag.debug      — halt/resume, GPR/CSR, memory r/w, diag  (the full debugger)

    from espjtag import EspUsbJtag
    j = EspUsbJtag()             # first 303a:1001 on the bus (or pass a usb_path)
    print(hex(j.read_idcode()))  # 0x0000dc25 on a C6
    j.examine(); j.halt()
    print(hex(j.read_register(0x1000 + 1)))   # x1 / ra
    print(hex(j.read_mem32(0x42000000)))      # memory-mapped flash
    j.resume()

    from espjtag.reset import reset_run        # the minimal reboot fix
    reset_run()
"""
from .transport import EspUsbJtagTransport
from .debug import EspUsbJtag, diag, selftest
from .reset import reset_run

__all__ = ["EspUsbJtag", "EspUsbJtagTransport", "reset_run", "diag", "selftest"]
__version__ = "0.2.0"
