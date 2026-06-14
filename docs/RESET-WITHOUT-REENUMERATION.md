# Resetting ESP32 RISC-V without USB re-enumeration (and without the buttons)

**The common problem:** on an ESP32-C3/C5/C6/H2 connected over the built-in
**USB-Serial/JTAG**, the usual ways to reset or reboot the chip drop the USB
device off the bus and force a **re-enumeration**. That:

- makes the next tool (OpenOCD, probe-rs, another esptool, a monitor) **race the
  re-enumeration** and fail — e.g. OpenOCD dies in ~20 ms with
  `libusb_get_string_descriptor_ascii() failed with -9` (`LIBUSB_ERROR_PIPE`),
  then `Unsupported DTM version: -1` / `Could not identify target`;
- costs **~450 ms every transition** waiting for the device to come back;
- on Linux, churns the tty (`/dev/ttyACM*` number can change), breaking scripts
  pinned to a port path;
- and — the part most people hit first — leaves users **reaching for the physical
  BOOT / EN(RST) buttons** to get into download mode or to reboot the app,
  because the software reset "didn't take." On a soldered-down or enclosed board
  (or a remote rig) there are no buttons to reach.

**espjtag fixes this at the source.** espjtag resets the chip over **JTAG**, by
pulsing the RISC-V Debug Module's `ndmreset` — a full-system reset that
re-samples the BOOT strap and boots the app **without dropping USB**. Same device,
same bus address, no re-enumeration, no race, no buttons.

## Why the usual resets re-enumerate (root cause)

The ESP32-C-series USB-Serial/JTAG (USJ) peripheral is **inside the chip's main
reset domain**. So a *full hardware/system reset* takes the USB peripheral down
with it → the host sees a disconnect + reconnect (re-enumeration). The two reset
*classes* behave oppositely:

| reset class | how it's triggered | USB effect |
|---|---|---|
| **full HW/system reset** | RTS/DTR pin toggle (esptool "classic" `hard-reset`), the RST/EN button, watchdog/system reset | USJ is in the reset domain → **re-enumerates** |
| **CPU/core-only reset** | the debug module's `ndmreset` over JTAG (what espjtag and OpenOCD's `reset` do); esptool's USB-aware reset on chips that support it | USJ **stays enumerated** — no disconnect |

Evidence (maintainer + source):
- openocd-esp32 **#316** — maintainer: *"when you hard reset the board, the
  usb-serial-jtag peripheral also will be under reset. This is a hardware design."*
  Reset from the monitor (`Reset cause 21 — USB (UART) core reset`) reconnects
  fine; the **RST button** gives `LIBUSB_ERROR_NO_DEVICE`/`-IO`.
- openocd-esp32 **#342** — the accepted fix is to *let OpenOCD own the reset*
  (`init; reset halt`) instead of a preceding hard-reset.
- esptool source: ESP32-C3/C6 `--after hard-reset` inherits the **classic RTS
  branch** (`reset.py` `HardReset`, `uses_usb=False`) → re-enumerates. C6
  `watchdog-reset` is even disabled in esptool because *"a bug in the
  USB-Serial/JTAG controller can cause the port to disappear."*
- esptool **#970** — the flip side: on USJ, esptool's post-flash reset is a no-op
  for *booting the app* (the core reset doesn't re-sample the BOOT strap), so the
  chip can sit in ROM download mode after a flash. Pulsing `ndmreset` boots it.

## What espjtag provides

```python
from espjtag.reset import reset_run

# Boot the app over JTAG — no USB re-enumeration, no buttons.
reset_run(serial="58:E6:C5:11:B7:EC")     # pin by stable MAC (preferred)
reset_run(usb_path="1-1.3.1.3.4")         # or by bus-port path
```

- Pin by **`serial`** (the USB MAC) — stable across re-enumeration and tolerant of
  format: `58:E6:C5:11:B7:EC`, `58e6c511b7ec`, lower/upper case all match.
- It's **transport-only** (no halt/registers/flash) — deliberately small so it can
  be lifted into a minimal esptool PR (see below).
- For a chip stuck in **post-flash ROM download** (BOOT strap latched low), a plain
  `reset run` lands back in ROM; use `--reset-from-rom` (USB-bus reset to clear the
  latch **+** ndmreset), or the API `EspUsbJtag.reset_run_from_rom()`.

Or from the command line (no script, works anywhere espjtag is installed):

```
python -m espjtag --reset-run       --serial 58:E6:C5:11:B7:EC   # boot the app
python -m espjtag --reset-from-rom  --serial 58:E6:C5:11:B7:EC   # out of ROM dl
```

## The esptool + espjtag combo (the recommended workflow)

Flash with esptool but **don't let it hard-reset**; boot with espjtag:

```
esptool --chip esp32c6 --port /dev/ttyACM0 --after no-reset write-flash 0x0 app.bin
python -c "from espjtag.reset import reset_run; reset_run(serial='58:E6:C5:11:B7:EC')"
```

`--after no-reset` keeps USB up (no re-enumeration); `reset_run` boots the app over
JTAG (also no re-enumeration). The device never leaves the bus — so a following
OpenOCD/probe-rs/monitor attaches cleanly with no wait and no race.

## Measured benefit

On c6-xiao-c (ESP32-C6, built-in USJ):

| reset | time | USB |
|---|---|---|
| esptool `--after hard-reset` (RTS) | **533 ms** | re-enumerates |
| espjtag `reset_run` (JTAG ndmreset) | **82 ms** | stays enumerated |

**7× faster, ~451 ms saved per reset**, and the `libusb -9` / `DTM -1` race is
eliminated (0 occurrences across the bench's mixed-tool runs after the switch).

## For the PR

This belongs in the espjtag → esptool contribution because it's a problem a large
fraction of USB-Serial/JTAG users hit (the button-reaching tell), the root cause
is documented upstream (#316/#342/#970), and the fix is small, measured, and
transport-only. Include: this doc, the `reset_run(serial=...)` entry point, the
`norm_serial` tolerance, and the measured 7×/451 ms + race-elimination evidence.
