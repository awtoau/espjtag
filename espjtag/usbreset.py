"""espjtag.usbreset — the MINIMAL, cross-platform USB *bus*-reset layer.

A USB bus reset re-enumerates a device on the wire. espjtag needs it for one
load-bearing reason: on a freshly-flashed ESP32-C6 left in USB-Serial/JTAG ROM
download mode, the BOOT strap is latched LOW and a bare RISC-V `ndmreset` (a core
reset) does NOT re-sample it — only re-enumerating the USJ peripheral clears that
download latch (see espjtag/debug.py reset_run_from_rom and esptool #970). So the
ROM-boot flow is: USB bus reset (here) -> ndmreset+resume handshake (debug.py).

This module is deliberately tiny and depends on NOTHING in espjtag except pyusb,
so it can be lifted into a minimal esptool reset PR alongside espjtag.reset.

==============================  PORTABILITY  ==============================
We do the reset through **pyusb's `dev.reset()`**, which calls libusb's
`libusb_reset_device()`. That ONE call is cross-platform — it is the right
portable primitive, and what the libusb backend actually runs per OS is:

  * Linux   -> the USBDEVFS_RESET ioctl on /dev/bus/usb/BBB/DDD. This is the
               EXACT same kernel call the old hand-rolled ioctl in this repo
               made (esp32-zephyr scripts/dev/power.py usb_reset), so on Linux
               `dev.reset()` is byte-for-byte equivalent to the raw ioctl — no
               behavioural change, just no sysfs/ioctl plumbing in our code.
  * Windows -> IOCTL_USB_RESET via the WinUSB/libusb driver bound to the JTAG
               vendor interface (see docs/CROSS-PLATFORM-USB.md — the 0xFF iface
               needs WinUSB; Zadig or a bundled .inf installs it).
  * macOS   -> libusb's reset path. CAVEAT: `libusb_reset_device()` has been a
               SILENT NO-OP on macOS since 10.11 El Capitan (libusb #455) — it
               returns success without actually re-enumerating. So the strap
               clear that ROM-boot depends on may NOT happen on macOS. We surface
               this rather than pretend; callers on macOS should treat ROM-boot
               as unverified (re-plug, or esptool --after watchdog-reset).

After a real reset that changes/re-enumerates the device, libusb returns
LIBUSB_ERROR_NOT_FOUND ("device disconnected/reconnected; the handle is no longer
valid — rediscover it"). pyusb raises that as usb.core.USBError. For a *bus*
reset that re-enumerates, that error is EXPECTED and benign — the device comes
back as a fresh node and the caller reopens it by (bus, port_numbers). So
reset_device() below treats a post-reset USBError as success-with-reenumerate,
not failure.

We keep a Linux raw-ioctl path ONLY as an explicit, opt-in fallback
(reset_device(..., allow_linux_ioctl_fallback=True)) for the case where the
libusb backend is unavailable; the default path is the portable pyusb one on
every OS. Pinned upstream + provenance: ../upstream.lock, ../ACKNOWLEDGEMENTS.md.
"""

import platform

import usb.core
import usb.util

# True on the only OS where the raw USBDEVFS_RESET ioctl exists.
IS_LINUX = platform.system() == "Linux"
# macOS libusb_reset_device is a documented silent no-op (libusb #455).
IS_MACOS = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

# Linux USBDEVFS_RESET ioctl number = _IO('U', 20). Used ONLY by the opt-in
# fallback below; the portable path never needs it (dev.reset() makes this exact
# ioctl itself inside libusb on Linux).
USBDEVFS_RESET = (ord("U") << 8) | 20


def reset_device(dev, log=None, allow_linux_ioctl_fallback=False):
    """USB *bus*-reset `dev` (a pyusb Device) so it re-enumerates — cross-platform.

    Returns a short string describing which path was taken ("pyusb",
    "pyusb(reenumerated)", "linux-ioctl") so callers/logs can see the OS path.

    Behaviour notes (see module docstring for the full per-OS story):
      * The portable path is pyusb `dev.reset()` == libusb_reset_device(). After
        a re-enumerating reset, libusb returns LIBUSB_ERROR_NOT_FOUND -> pyusb
        USBError; that is EXPECTED here (the device comes back fresh) and is
        treated as success, not an error.
      * On macOS this may be a SILENT NO-OP (libusb #455) — the call "succeeds"
        but the device is not actually reset. We cannot detect that from here;
        the caller's re-open will simply find the un-reset device. Documented,
        not worked around.
      * `allow_linux_ioctl_fallback` only does anything on Linux, and only if the
        pyusb path itself raises something OTHER than the benign re-enumerate
        USBError (e.g. the libusb backend is missing). It is OFF by default —
        the portable call is preferred everywhere.

    This call INVALIDATES `dev`'s handle (the device re-enumerates as a new node).
    The caller must dispose `dev` and re-open the unit by its stable identity
    (bus + port_numbers), which survives a bus reset. We do NOT reopen here —
    that is the transport/debug layer's job (it knows how to match the unit)."""
    def _log(m):
        if log:
            log(m)

    try:
        dev.reset()                      # libusb_reset_device — portable
        return "pyusb"
    except usb.core.USBError as e:
        # A re-enumerating bus reset makes libusb return LIBUSB_ERROR_NOT_FOUND
        # (handle no longer valid). For our purpose (we WANT re-enumeration) that
        # is the success case, not a failure.
        _log(f"    (dev.reset() raised {e}; device re-enumerated — expected)")
        return "pyusb(reenumerated)"
    except (NotImplementedError, AttributeError) as e:
        # The active backend can't reset (e.g. no libusb). Optionally fall back
        # to the raw Linux ioctl; otherwise re-raise so the caller sees it.
        _log(f"    (pyusb reset unavailable: {e})")
        if IS_LINUX and allow_linux_ioctl_fallback:
            _linux_ioctl_reset(dev, log=log)
            return "linux-ioctl"
        raise


def _linux_ioctl_reset(dev, log=None):
    """LINUX-ONLY raw USBDEVFS_RESET fallback. Equivalent to what dev.reset()
    runs inside libusb on Linux — kept only for the no-libusb-backend case. Off
    Linux this raises; never call it there.

    Resolves the device's /dev/bus/usb/BBB/DDD node from the live bus/address,
    opens it, and issues the ioctl. The handle is invalidated by the reset; the
    caller reopens by (bus, port_numbers)."""
    if not IS_LINUX:
        raise RuntimeError("USBDEVFS_RESET ioctl is Linux-only; use dev.reset()")
    import fcntl

    busnum = dev.bus
    devnum = dev.address
    node = f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"
    if log:
        log(f"    linux fallback: USBDEVFS_RESET on {node}")
    fd = open(node, "wb")
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    finally:
        fd.close()


def platform_reset_note():
    """One-line human-readable note about what a USB bus reset does on THIS OS.
    Useful for diagnostics/CLI so the per-OS behaviour is never a surprise."""
    if IS_LINUX:
        return ("Linux: dev.reset() -> USBDEVFS_RESET ioctl (re-enumerates; "
                "same call as the legacy raw ioctl).")
    if IS_WINDOWS:
        return ("Windows: dev.reset() -> IOCTL_USB_RESET via WinUSB/libusb "
                "(re-enumerates). The JTAG 0xFF iface needs a WinUSB driver "
                "(Zadig/.inf) — see docs/CROSS-PLATFORM-USB.md.")
    if IS_MACOS:
        return ("macOS: dev.reset() may be a SILENT NO-OP (libusb #455, >=10.11) "
                "— the C6 strap-clear it relies on is UNVERIFIED here; prefer "
                "re-plug or esptool --after watchdog-reset.")
    return f"{platform.system()}: dev.reset() -> libusb_reset_device (untested)."
