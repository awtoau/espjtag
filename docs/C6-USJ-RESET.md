# ESP32-C6 won't boot the app after flash (USB-Serial/JTAG) — and the fix

## Symptom
After flashing an ESP32-C6 over its native USB-Serial/JTAG, the chip stays in ROM
**download mode** — console silent, app not running. `esptool --before no-reset
chip-id` still connects (chip is alive, just stuck). Only a physical replug
appeared to recover it.

## Root cause (Espressif docs + esptool #970)
The BOOT strap (GPIO9) is **latched in hardware and only re-sampled on a
full-system reset** (power-on, RTC-watchdog *chip* reset, brownout). The C6's USJ
peripheral can only trigger a **core reset**, which does **not** re-sample the
strap — so the chip stays in download mode.

Therefore, over USJ on the C6, none of these boot the app:
- `esptool --after hard-reset` — toggles RTS, which doesn't exist over USJ → no-op.
- `esptool --after watchdog-reset` — **disabled on the C6** by esptool (it can
  freeze the USJ port); falls back to the no-op hard-reset.
- The `USBJTAGSerialReset` DTR/RTS sequence, `esptool run`, `USBDEVFS_RESET` —
  all are core-level / don't re-strap. Tested: none recover it.

**The C5 differs:** the C5 target keeps `--after watchdog-reset` (it has a
`hard_reset` override), so the C5 flash flow boots the app. Do **not** copy the C6
handling onto the C5.

## The fix — OpenOCD `reset run` over the built-in USB-JTAG
OpenOCD attaches to the RISC-V debug module over the same 303a:1001 USB-JTAG and
drives the core directly, **bypassing the strap decision** — so it boots the app
without a replug. Verified on xiao-c6 (E4:B0), stuck in ROM → running the app.

`dev.py` does this automatically: after a C6 `usb-flash`, it runs
`oocd_reset_run()` (scripts/dev/flash.py) — OpenOCD `init; reset run`, pinned to
the unit's USB location.

### Pure-Python alternative — espjtag `reset_run_from_rom()` (replaces OpenOCD)

OpenOCD is **not** the only thing that can do this. `espjtag`'s
`reset_run_from_rom()` reproduces the same recipe in pure Python — but the recipe
is a **combination**, and the distinction matters:

- A **bare ndmreset** (what `espjtag.reset.reset_run()` does) does **not** boot a C6
  out of post-flash ROM download (verified **0/3**; even OpenOCD's *bare* `reset run`
  is 0/3 from that state). A core reset doesn't re-sample the strap.
- A **USB bus reset alone** (USBDEVFS_RESET) is also 0/3 (clears the download latch
  but leaves the core parked).
- **The combination is 3/3:** USB bus reset (clear the BOOT-strap-low download latch)
  **+** the full SoC reset-register / ndmreset / deassert / halt → dcsr → resume
  handshake. That is exactly what `reset_run_from_rom()` does, and it is bench-proven
  on a C6. (See
  [ESPJTAG-STORY.md](ESPJTAG-STORY.md),
  [GIT-HISTORY-IDEAS.md](GIT-HISTORY-IDEAS.md) §3b.)

So `dev.py` **still delegates to OpenOCD** here as the long-established proven path,
while the pure-Python `reset_run_from_rom()` is bench-verified and the switch-over is
pending. Once switched, no OpenOCD binary is needed for flash+boot — just the USB
cable and Python.

Manual form (recover a stuck C6 by hand):
```sh
OOCD=~/.espressif/tools/openocd-esp32/v0.12.0-esp32-20251215/openocd-esp32/bin/openocd
SCR=~/.espressif/tools/openocd-esp32/v0.12.0-esp32-20251215/openocd-esp32/share/openocd/scripts
# USB path from: dev.py --target xiao-c6 find   (the "USB path" line)
"$OOCD" -s "$SCR" -c "adapter usb location 1-1.3.1.3.1" \
        -f board/esp32c6-builtin.cfg -c "init; reset run; exit"
```

### Gotchas
- **Must pin `adapter usb location <path>`** — there are many 303a:1001 on the
  bench bus; without the pin OpenOCD grabs the wrong tap
  (`Unsupported DTM version: -1 / Could not identify target type`).
- Uses the **Espressif-patched OpenOCD** (ships with ESP-IDF), not vanilla /
  Zephyr-SDK OpenOCD (no ESP support).
- Cosmetic `LIBUSB_ERROR_IO` / polling noise after reset on the C6 USB-JTAG
  (openocd-esp32 #316) — the app still runs.

### Firmware-side prevention (future)
A robust fix is to clear `RTC_CNTL_OPTION1_REG` (the sticky force-download bit)
and provoke a chip-level RTC-WDT reset (set `WDT_CHIP_RESET_EN` +
`WDT_CHIP_RESET_WIDTH`) on the app's "reboot to bootloader/app" path, so resets
re-sample the strap in software. Not yet implemented — tracked as
[espjtag#2](https://github.com/awtoau/espjtag/issues/2).

## Refs
esptool #970, esp-idf #13287, esptool C6 troubleshooting docs, openocd-esp32 #316,
ESP32-C6 errata RES-7080.
