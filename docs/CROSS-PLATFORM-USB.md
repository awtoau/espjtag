# Cross-platform USB (Windows + Linux + macOS) — espjtag #13

espjtag talks to the ESP32 built-in USB-Serial/JTAG (`303a:1001`) through
[pyusb] over libusb. libusb is cross-platform, so the *bulk JTAG I/O* already
works on all three OSes. The two things that were Linux-only — the USB **bus
reset** (used to clear the C6 ROM-download strap latch) and the **kernel-driver
detach** — are addressed here. This doc records what each OS does, what a Windows
user must install, and what still needs bench verification.

## TL;DR

| Concern | Linux | Windows | macOS |
|---|---|---|---|
| Bulk JTAG (vendor iface 0xFF) | ✅ libusb | ⚠️ needs WinUSB driver bound to the 0xFF iface (Zadig / `.inf`) | ✅ libusb |
| USB bus reset (`dev.reset()`) | ✅ → `USBDEVFS_RESET` ioctl | ✅ → `IOCTL_USB_RESET` (WinUSB) | ❌ silent no-op since 10.11 (libusb #455) |
| Kernel-driver detach | ✅ done (cdc_acm/usbhid) | n/a — skipped | n/a — skipped |
| C6 ROM-boot (`reset_run_from_rom`) | ⚠️ **must re-verify on bench** | ❓ untested | ❌ expected not to work (reset is a no-op) |

## 1. The portable USB reset — pyusb `dev.reset()`

The reset lives in [`espjtag/usbreset.py`](../espjtag/usbreset.py)
(`reset_device()`), and `espjtag/debug.py reset_run_from_rom()` calls it.

`pyusb`'s `Device.reset()` calls libusb's **`libusb_reset_device()`**. That single
call is the portable primitive; the libusb backend does the OS-appropriate thing:

- **Linux** → the `USBDEVFS_RESET` ioctl on `/dev/bus/usb/BBB/DDD`. This is the
  *exact same kernel call* the legacy hand-rolled ioctl made (esp32-zephyr
  `scripts/dev/power.py usb_reset`), so on Linux switching to `dev.reset()` is
  byte-for-byte equivalent — no behaviour change, no sysfs/ioctl plumbing in our
  code.
- **Windows** → `IOCTL_USB_RESET` through the WinUSB/libusb driver bound to the
  interface. Re-enumerates the device.
- **macOS** → libusb's reset path, **but** `libusb_reset_device()` has been a
  *silent no-op on macOS since 10.11 El Capitan* ([libusb #455]). It returns
  success without actually re-enumerating. We do not pretend otherwise — see §3.

### Why `dev.reset()` and not the raw ioctl

The old approach (in the sibling esp32-zephyr repo, and conceptually in the brief)
was a Linux-only `fcntl.ioctl(fd, USBDEVFS_RESET)` on a sysfs-resolved
`/dev/bus/usb/...` node. That assumes Linux sysfs paths and the Linux usbfs API.
`dev.reset()` reaches the **same ioctl on Linux** while also working on
Windows/macOS, with no `/dev/bus/usb` assumption in the library. We keep a raw
Linux ioctl only as an **opt-in fallback**
(`reset_device(..., allow_linux_ioctl_fallback=True)`) for the unusual case where
the libusb backend can't reset; the default everywhere is the portable call.

### Re-enumeration is expected, not an error

A re-enumerating bus reset makes libusb return `LIBUSB_ERROR_NOT_FOUND` — "the
device disconnected/reconnected, the handle is no longer valid, rediscover it."
pyusb raises that as `usb.core.USBError`. Since we *want* the device to
re-enumerate (that is how the strap latch clears), `reset_device()` treats a
post-reset `USBError` as **success-with-re-enumerate**, not failure. The caller
disposes the handle and re-opens the unit by its stable `(bus, port_numbers)`
identity (the bus address changes across a reset; the physical port path does
not — this is the `port_numbers` matching key, the same one the awto-usb Rust
sample captures).

## 2. Windows: the JTAG vendor interface needs a WinUSB driver

The `303a:1001` device is **composite**:

- interfaces 0/1 = the **CDC-ACM console** (classes 0x02 / 0x0a). On Windows these
  bind to the **inbox `usbser.sys`** driver automatically — the COM port "just
  works", no install.
- the **vendor-specific JTAG interface (class 0xFF)** — what espjtag drives — has
  **no inbox driver on Windows**. libusb can only claim it once a
  **WinUSB-family** driver (WinUSB, libusb-win32, or libusbK) is bound to *that
  interface*.

How to bind it (any one of):

- **[Zadig]** — point it at the `303a:1001` device, select the JTAG/vendor
  interface (composite interface 2, class 0xFF — *not* the CDC interfaces), and
  install **WinUSB**. This is the standard libusb-on-Windows path.
- **A bundled `.inf`** (WinUSB co-installer / `libwdi`) that targets
  `VID_303A&PID_1001&MI_02` so a packaged tool installs the driver without the
  user running Zadig by hand.

This is not espjtag-specific — it is how **esptool** and **OpenOCD** already run
over this device on Windows:

- Espressif's **OpenOCD-esp32** ships a Windows build and uses libusb; its docs
  instruct binding WinUSB to the JTAG interface (historically via Zadig; current
  Espressif tooling installs the driver). Same 0xFF interface, same requirement.
- **esptool** on Windows uses the **CDC console** (the COM port via `usbser.sys`),
  which is why esptool needs *no* WinUSB driver — it never touches the 0xFF
  interface. espjtag does, so it does.

So on Windows the split is clean: console = inbox `usbser.sys`; JTAG = WinUSB you
install once. espjtag's `transport.py` finds the 0xFF interface dynamically and
**skips the kernel-driver detach off Linux** (there is no kernel driver to detach
for a WinUSB-bound interface) — that part is already cross-platform.

## 3. macOS

- Bulk JTAG I/O over libusb works on macOS — no driver install needed for a
  vendor (0xFF) interface (macOS lets libusb claim it; no kext required for a
  device with no matching system driver).
- **`libusb_reset_device()` is a documented silent no-op on macOS ≥ 10.11**
  ([libusb #455]). It returns success but does not actually reset/re-enumerate.
  Consequence: the **C6 ROM-boot strap-clear cannot be relied on via `dev.reset()`
  on macOS.** `usbreset.platform_reset_note()` says so at runtime, and
  `reset_run_from_rom()` will likely fail to boot a download-mode C6 on macOS.
  Workarounds for macOS users: physically re-plug the device, or use
  `esptool --after watchdog-reset` (a full RTC-WDT reset re-samples strapping and
  also drops the USB link — esptool #970). espjtag does **not** silently claim the
  reset worked on macOS.

## 4. What still needs bench verification — REQUIRED before trusting this

> **`reset_run_from_rom()` on Linux MUST be re-verified on hardware (espjtag #13).**

The "boots a download-mode C6 out of ROM, 3/3" result was measured with the
*previous* code on Linux. Routing the reset through `usbreset.reset_device()` does
**not** change the underlying call on Linux (still `USBDEVFS_RESET` via libusb), so
the strap-clear *should* be identical — but this is load-bearing and must be
proven, not assumed:

1. Flash a C6 with `esptool ... --after no-reset` (leaves it in USB-Serial/JTAG
   ROM download mode, BOOT strap latched LOW).
2. Run `EspUsbJtag(...).reset_run_from_rom()`.
3. Confirm the app actually boots (console banner / heartbeat), repeated several
   times for confidence (the original bar was 3/3).

Until that passes, treat the Linux ROM-boot path as **unverified-after-refactor**.
Windows ROM-boot is entirely **untested**. macOS ROM-boot is **expected to fail**
(§3). The plain `reset_run()` (ndmreset only, no USB reset) is unaffected by this
change.

## 5. CI feasibility (note, optional)

The **non-hardware logic can import and unit-test on Windows/macOS GitHub
runners** without a board:

- `python -c "import espjtag"` should succeed on all three OSes — the only
  platform branch (`usbreset.IS_LINUX`/`IS_MACOS`/`IS_WINDOWS`) is evaluated at
  call time, not import time, and the kernel-driver detach is guarded.
- Importable + unit-testable without hardware: the JTAG TAP state machine
  (`transport.py` `_NEXT` graph + `_goto` BFS), the DMI word packing, the
  `usbreset.platform_reset_note()` strings, and the `usb_path` → `(bus,
  port_numbers)` matcher logic. None of these need a USB device.
- libusb must be present for `import usb.core` to find a backend, but **import**
  of espjtag does not *open* a device; constructing `EspUsbJtag()` does. So a CI
  matrix (`ubuntu`/`windows`/`macos`) can run an import smoke test + the pure
  logic unit tests. Anything that opens `303a:1001` or calls `reset_device()` on
  real hardware stays a manual bench step.

A small `windows`/`macos`/`ubuntu` import-smoke matrix is the cheap, high-value CI
add; full transport tests would need a self-hosted runner with a board attached.

[pyusb]: https://github.com/pyusb/pyusb
[Zadig]: https://zadig.akeo.ie/
[libusb #455]: https://github.com/libusb/libusb/issues/455
