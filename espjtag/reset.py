"""espjtag.reset — the MINIMAL reset-after-flash fix, depending ONLY on the
transport layer (not the full debugger). This is the piece that matters for
esptool: on the ESP32-C6 (and other RISC-V parts over USB-Serial/JTAG) esptool's
post-flash reset is a no-op — the USJ can only do a core reset, which doesn't
re-sample the BOOT strap, so the chip stays in ROM download mode after a flash
(esptool #970). Pulsing the RISC-V Debug Module's ndmreset IS a full-system reset
that re-straps and boots the app.

Kept deliberately small and dependency-light so it can be lifted into a minimal
esptool PR: it imports only the transport + a handful of constants — no halt,
registers, memory, or flash code.

Sequence mirrors OpenOCD's `reset run` (captured from its -d3 log):
    dmcontrol <- resumereq | dmactive
    dmcontrol <- haltreq | ndmreset | dmactive   (assert full-system reset)
    dmcontrol <- haltreq | dmactive              (deassert ndmreset, hold halted)
    dmcontrol <- ackhavereset | dmactive
    dmcontrol <- resumereq | dmactive            (run the freshly-strapped app)

dmcontrol bit offsets come from the RISC-V debug spec (openocd-esp32
src/target/riscv/debug_defines.h, BSD-2-Clause OR CC-BY-4.0). Pinned upstream:
../upstream.lock. Provenance + licensing: ../ACKNOWLEDGEMENTS.md.
"""

import time

import usb.util

from .constants import (
    DMCONTROL, DM_DMACTIVE, DM_NDMRESET, DM_ACKHAVERESET, DM_RESUMEREQ,
    DM_HALTREQ,
)
from .transport import EspUsbJtagTransport


def reset_run(usb_path=None):
    """Reboot the matching RISC-V ESP (C3/C5/C6/H2) into its app by pulsing
    ndmreset over the built-in USB-JTAG. Pure Python, no OpenOCD, transport-only.
    Returns True on a clean run."""
    j = EspUsbJtagTransport(usb_path)
    j.dmi_write(DMCONTROL, DM_DMACTIVE)                              # claim DM
    j.dmi_write(DMCONTROL, DM_RESUMEREQ | DM_DMACTIVE)
    j.dmi_write(DMCONTROL, DM_HALTREQ | DM_NDMRESET | DM_DMACTIVE)   # assert reset
    time.sleep(0.05)
    j.dmi_write(DMCONTROL, DM_HALTREQ | DM_DMACTIVE)                 # deassert ndmreset
    j.dmi_write(DMCONTROL, DM_ACKHAVERESET | DM_DMACTIVE)            # ack
    j.dmi_write(DMCONTROL, DM_RESUMEREQ | DM_DMACTIVE)              # RUN
    j.dmi_write(DMCONTROL, 0)                                        # release DM
    usb.util.dispose_resources(j.dev)
    return True
