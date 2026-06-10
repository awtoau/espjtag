# esptool ↔ espjtag integration — design plan (PLAN ONLY, no code)

Integrate the pure-Python RISC-V JTAG **ndmreset** reset (from `espjtag`,
`/home/dan/git/espjtag`) into the esptool fork (`awtoau/esptool`, cloned at
`/home/dan/git/esptool-fork`) so that:

1. the **ESP32-C6 reset-after-flash "just works"** over the built-in
   USB-Serial/JTAG (USJ), without OpenOCD and without a replug;
2. esptool is callable as a **Python library**, not a subprocess, so
   esp32-zephyr's `scripts/dev/*.py` can `import esptool` and drive flash + the
   JTAG reset in-process;
3. there is a **minimal, upstream-PR-able boundary**: one new `ResetStrategy`
   subclass + a selection hook + a C6 target change, nothing more.

All file:line references are into `/home/dan/git/esptool-fork/esptool/` unless
stated otherwise. This document is design only — no code was changed.

---

## 0. The one thing that must be resolved first (READ THIS)

> **RESOLVED — branch (B). See "Action Item 0 RESOLVED" at the end of this doc.**
> The bench test was run: a bare `espjtag.reset.reset_run()` (ndmreset pulse) does
> **NOT** boot a C6 out of post-flash ROM; the deeper sequence is required. So the
> JTAG-reset class must carry OpenOCD's full reset-run, not just the pulse. Note that
> espjtag's separate `reset_run_from_rom()` — the **combination** of a USB bus reset
> *and* the deep handshake — *does* boot from ROM (3/3, bench-proven); it's
> `reset_run()` alone that can't. The contradiction below was the symptom; this is the
> resolution. The framing of "(A) vs (B)" is kept for the reasoning trail.

There was a **direct contradiction in the repos** about whether the
pure-Python ndmreset actually boots a C6 out of *post-flash ROM download mode*
(resolved — see the banner; branch B held):

- `espjtag/espjtag/reset.py:1-19` (docstring) and `reset.py:32-46` (`reset_run()`)
  **claim** the ndmreset pulse "IS a full-system reset that re-straps and boots
  the app," mirroring OpenOCD `reset run`.
- `espjtag/README.md:66` **contradicts** this: *"Boot a chip out of post-flash
  ROM download — ⚠️ use OpenOCD (deeper sequence)."* `reset_run()` is only listed
  as "reboot a **running** core" (`README.md:65`).
- esp32-zephyr `scripts/dev/flash.py:371-407` is the **ground-truth bench result**:
  `c6_reset_run()` explicitly delegates to `_oocd_reset_run()` (OpenOCD), with the
  comment *"[espjtag] resets a RUNNING core, but does NOT yet boot a chip from the
  post-flash ROM state — that needs the deeper halt/examine/resume reset-run
  sequence OpenOCD does. So OpenOCD is the PROVEN post-flash boot path
  (aggressive-tested 3/3)."*
- `docs/C6-USJ-RESET.md` documents the same: OpenOCD `reset run` is the verified
  un-stick; the pure-Python path is roadmap.

**Implication for this plan.** The *integration mechanics* below (new
`ResetStrategy`, selection hook, C6 target change, library API, USB-interface
coexistence) are all correct and ready to implement **regardless**. But the
**acceptance gate** — "after `write_flash` + JTAG reset, the C6 actually RUNS the
app from the post-flash ROM-download state" — is **not yet proven** with the
current 7-write `reset_run()` sequence. Two possible truths, and the first bench
test must decide which:

- **(A)** The newer `reset.py` sequence (it now includes `haltreq` and an
  `ackhavereset` step at `reset.py:39-43`, closer to OpenOCD's) *does* boot from
  ROM, and the README/`flash.py` comments are stale. → Integration is a
  straight port; ship it.
- **(B)** It still doesn't (matches the bench result). → The minimal PR's reset
  class must carry the *deeper* sequence (examine → halt → ndmreset → ack →
  resume, as OpenOCD does), i.e. depend on a bit more than `espjtag.transport`,
  OR the C6 post-flash boot stays on OpenOCD and the JTAG-reset PR only claims
  "reboot a running C6" (still useful: `esptool --after`, not the flash-boot).

> **Action item 0:** on the bench, flash a C6 to ROM-download, then call
> `espjtag.reset.reset_run()` alone (no OpenOCD) and check the console boots.
> This single test selects branch (A) vs (B) and decides the PR's scope. Until
> then, treat the "boots the app" claim as **unverified**.

The rest of this document is written so it holds under either branch; where (B)
changes a decision it is called out.

---

## 1. How esptool selects a reset strategy today

Two distinct mechanisms — do not conflate them:

### 1a. *Connect-time* reset (`--before`) — get INTO the bootloader
`ESPLoader._construct_reset_strategy_sequence(mode)`
(`loader.py:793-835`) builds the sequence tried during `connect()`
(`loader.py:837-869`, called per-attempt at `loader.py:865-869`). Selection:

- custom sequence from config → `CustomReset` (`loader.py:799-801`);
- **`mode == "usb-reset"` OR `get_usb_vid_pid()[1] == USB_JTAG_SERIAL_PID`
  (0x1001) → `USBJTAGSerialReset` (`loader.py:818-819`)** — this is the
  PID-based USJ branch;
- CP2102C hardware-flow-control adapters get `flow_control=True`
  (`loader.py:821`, `uses_hardware_flow_control()` at `loader.py:1253-1259`,
  PID table at `loader.py:379-381`);
- else Unix → `UnixTightReset`×2 + `ClassicReset`×2 (`loader.py:824-830`);
- else (Windows / rfc2217) → `ClassicReset`×2 (`loader.py:832-834`).

`USB_JTAG_SERIAL_PID = 0x1001` is at `loader.py:378`; `ESPRESSIF_VID = 0x303A` at
`loader.py:376`. `get_usb_vid_pid()` (`loader.py:689-727`) resolves VID/PID by
matching the **serial port path** against `serial.tools.list_ports.comports()` —
it does NOT open the USB device itself.

### 1b. *After-operation* reset (`--after`) — what we care about
The `--after` value (default `hard-reset`, `__init__.py:402-410`; choices
`hard-reset | soft-reset | no-reset | no-reset-stub | watchdog-reset`) is stashed
in the click context and applied in `teardown()` (`__init__.py:624-645`) via
`reset_chip(esp, ctx.obj["after"])` (`__init__.py:639`).

`reset_chip()` (`cmds.py:2416-2453`) is a flat `if/elif` dispatch on the
mode string → calls a method on the `esp` object:
- `hard-reset` → `esp.hard_reset()` (`cmds.py:2431-2432`);
- `watchdog-reset` → `esp.watchdog_reset()` (`cmds.py:2439-2447`);
- `soft-reset` / `no-reset` / `no-reset-stub` → soft/no-op (`cmds.py:2433-2451`).

The base `ESPLoader.hard_reset()` (`loader.py:2057-2065`) drives `HardReset`
(RTS/DTR over the serial line). The base `watchdog_reset()`
(`loader.py:2090-2095`) just warns and falls back to `hard_reset()`.

### 1c. The C5-vs-C6 asymmetry (the bug we're fixing)
- **C5** overrides `hard_reset()` to pass `uses_usb_jtag_serial()`
  (`targets/esp32c5.py:142-143`) and *re-enables* `watchdog_reset()`
  (`targets/esp32c5.py:225-227`, delegating to the C3 implementation
  `targets/esp32c3.py:244-252` which writes the RTC-WDT registers). So
  `--after watchdog-reset` boots the app on the C5.
- **C6** overrides `watchdog_reset()` to **disable** it
  (`targets/esp32c6.py:208-211`): *"Bug in the USB-Serial/JTAG controller can
  cause the port to disappear if watchdog reset happens, disable it on
  ESP32-C6"* → it falls back to the base no-op `hard_reset()` over RTS, which over
  USJ does nothing. **The C6 has no working `--after` that boots the app over
  USJ.** This is esptool #970 and the gap we close.

So: the C6 (and C3/H2 with the same USJ limitation) needs a *new* post-flash
reset method that is a real full-system reset — the ndmreset pulse.

---

## 2. The integration: new ResetStrategy + selection hook + C6 target change

### 2a. New strategy class — `JTAGSystemReset` in `reset.py`
Add one class to `esptool/reset.py` (alongside `ClassicReset`,
`USBJTAGSerialReset`, `HardReset` — `reset.py:105-205`). Unlike its siblings it
does **not** drive `self.port`'s DTR/RTS; it opens the **vendor JTAG interface**
of the same physical USB device and pulses ndmreset. Shape:

```text
class JTAGSystemReset(ResetStrategy):
    """Full-system reset over the built-in USB-Serial/JTAG vendor interface.
    Pulses the RISC-V Debug Module ndmreset (esp_usb_jtag protocol) so the
    chip re-samples the BOOT strap and boots the app — the one reset that
    works on the C6 over USJ, where RTS/DTR and core-only resets are no-ops
    (esptool #970). RISC-V parts only (C3/C5/C6/H2)."""
    def reset(self):
        # locate the SAME 303a:1001 device as self.port (by USB topology),
        # claim the 0xFF vendor interface, run the ndmreset sequence.
```

Implementation options for the body (decide by Action item 0 / §6):
- **Vendored**: copy `espjtag/transport.py` + `reset.py` (transport + the ndmreset
  sequence) into esptool, e.g. `esptool/reset_jtag.py`. Keeps esptool
  dependency-free of an external `espjtag` package — important for an upstream PR.
- **Optional import**: `try: from espjtag.reset import reset_run` and degrade
  gracefully if absent. Cleaner for *our* fork, but adds a dependency upstream
  won't accept. → Use **vendored** for the upstream PR; the optional-import form
  is fine for our internal fork if we prefer.

`ResetStrategy.__init__` already stores `self.port` and tolerates the retry/close
loop in `__call__` (`reset.py:38-64`); a JTAG reset that doesn't touch DTR/RTS
won't hit the `ENOTTY/EINVAL` path. Note `__call__` reopens `self.port` if closed
(`reset.py:46-49`) — see §5 on coexistence.

### 2b. Where it plugs into the `--after` dispatch
Three steps, all tiny:

1. **CLI choice** — add `"jtag-reset"` to the `--after` `ResetModeType` list at
   `__init__.py:405-407` (currently
   `["hard-reset","soft-reset","no-reset","no-reset-stub","watchdog-reset"]`).
2. **`reset_chip()` dispatch** — add a branch in `cmds.py:2416-2453`:
   `elif reset_mode == "jtag-reset": esp.jtag_system_reset()`. Update the
   docstring (`cmds.py:2417-2429`).
3. **Loader method** — add `ESPLoader.jtag_system_reset()` near
   `hard_reset()`/`watchdog_reset()` (`loader.py:2057-2095`). Base implementation
   should *fall back* gracefully (warn + `hard_reset()`) for chips/connections
   with no JTAG — mirroring how `watchdog_reset()` warns and falls back
   (`loader.py:2090-2095`). The RISC-V USJ targets override it to actually do the
   ndmreset.

### 2c. The C6 target change (the heart of the fix)
In `targets/esp32c6.py` (class `ESP32C6ROM`, `esp32c6.py:14`), add an override:

```text
def jtag_system_reset(self):
    if self.uses_usb_jtag_serial():
        JTAGSystemReset(self._port)()       # real full-system reset
    else:
        ESPLoader.hard_reset(self)          # external UART bridge: normal RTS
```

`uses_usb_jtag_serial()` (`loader.py:1241-1245`) is the exact guard — it's the
PID==0x1001 check. This is the single place the C6 "gains a JTAG-system-reset
`--after` behaviour," directly analogous to how the C5 added its `hard_reset`
override at `targets/esp32c5.py:142-143`.

Because `ESP32C5ROM(ESP32C6ROM)` (`targets/esp32c5.py:16`) and
`ESP32C3ROM`→C6 share inheritance, putting `jtag_system_reset()` on `ESP32C3ROM`
(`targets/esp32c3.py:15`) would cover **C3, C6, C5, H2** in one edit (they all
share the USJ + RISC-V Debug Module). Recommended for the broadest fix with the
fewest lines — but confirm H2 idcode/DM compatibility on the bench first.

### 2d. Auto-selection (optional, more controversial for upstream)
Beyond an explicit `--after jtag-reset`, we *could* make the C6 silently choose
the JTAG reset when `uses_usb_jtag_serial()` and `--after hard-reset` was
requested (since hard-reset is a known no-op there). That's a behaviour change to
the default and upstream will scrutinise it. **Keep it out of the minimal PR.**
For our fork it's reasonable; gate it clearly. The clean upstream story is: "new
opt-in `--after jtag-reset`; C6 docs recommend it."

---

## 3. Minimal upstream-PR boundary

Smallest mergeable change to `espressif/esptool` (or our fork → upstream):

| # | File | Change | Lines (approx) |
|---|------|--------|----------------|
| 1 | `esptool/reset_jtag.py` *(new)* | Vendored esp_usb_jtag transport + `JTAGSystemReset` ndmreset class. Pure pyusb. | ~250 (port of espjtag transport.py + reset.py + constants.py) |
| 2 | `esptool/reset.py` | `from .reset_jtag import JTAGSystemReset` (or define the class here) | ~1 |
| 3 | `esptool/cmds.py` | `reset_chip()` `elif "jtag-reset"` branch + docstring (`cmds.py:2416-2453`) | ~3 |
| 4 | `esptool/loader.py` | base `jtag_system_reset()` (warn + fallback), near `loader.py:2090` | ~6 |
| 5 | `esptool/targets/esp32c3.py` | `jtag_system_reset()` override guarded by `uses_usb_jtag_serial()` (covers C3/C5/C6/H2 via inheritance) | ~6 |
| 6 | `esptool/__init__.py` | add `"jtag-reset"` to `--after` choices (`__init__.py:405-407`) | ~1 |

**Dependency:** `pyusb` is the only new runtime dep. esptool already depends on
`pyserial`; adding `pyusb` is the one thing to negotiate with upstream (it's
optional — import lazily inside `JTAGSystemReset.reset()` so esptool still imports
without it, and fall back if missing).

**Out of the minimal PR:** the full debugger (halt/mem/regs — `espjtag/debug.py`),
auto-selection of jtag-reset as the C6 default (§2d), and any flash-over-JTAG. The
PR is *only* "a new `--after jtag-reset` strategy that does a real full-system
reset over USJ, fixing the C6 post-flash boot (#970)."

**Licensing note:** `esptool/reset.py` is **GPL-2.0-or-later**
(`reset.py:1-4`); `espjtag` is **Apache-2.0** (README.md:84) but was itself ported
from `openocd-esp32` (**GPL-2.0**, README.md:75-79). Vendoring the transport into
GPL esptool is fine; keep the openocd-esp32 attribution. (esptool as a whole is
GPL-2.0-or-later, so the vendored file should carry that header to match.)

---

## 4. The Python-library API path (no subprocess)

esptool exposes a clean function-level API in `esptool/cmds.py`, re-exported from
`esptool/__init__.py` (`__init__.py:6-34`, `__all__`). You do **not** need to go
through `main()`/argparse. The relevant public functions:

- `detect_chip(port, baud, connect_mode, ...) -> ESPLoader` (`cmds.py:111-117`)
  — opens the port, resets into the bootloader, detects + returns the chip object.
- `attach_flash(esp, spi_connection=None, flash_type="nor")` (`cmds.py:1650-1668`)
  — must be called before any flash op (the CLI does this in `write_flash_cli`,
  `__init__.py:822`).
- `write_flash(esp, addr_data, flash_freq, flash_mode, flash_size, **kwargs)`
  (`cmds.py:870-917`) — `addr_data` is a `list[(addr, ImageSource)]` where
  ImageSource is a path/bytes/file-like.
- `run_stub(esp) -> ESPLoader` (`cmds.py:2456`) — optional, returns the stub
  loader object (faster flashing).
- `reset_chip(esp, reset_mode)` (`cmds.py:2416`) — after our change, accepts
  `"jtag-reset"`.

`main(argv=None, esp=None)` (`__init__.py:1242-1259`) also accepts a pre-built
`esp` object, but the function-level API is cleaner for us. The `esp` object holds
the open serial port at `esp._port`; `esp.serial_port` (`loader.py:492-494`)
returns the path string.

### 4a. Concrete in-process call sequence for dev.py

```text
import esptool
from esptool.cmds import detect_chip, attach_flash, write_flash, run_stub, reset_chip

# 1. detect + connect (replaces `esptool --chip ... --port ... --before ...`)
#    connect_mode "no-reset" if firmware already handed USB to ROM (our reboot_bl
#    path, flash.py:323-327); else "usb-reset" / "default-reset".
esp = detect_chip(port="/dev/serial/by-id/...-if00",
                  baud=460800, connect_mode="no-reset")

# 2. (optional) load the stub for speed
esp = run_stub(esp)

# 3. attach flash, then write
attach_flash(esp)
write_flash(esp, [(0x2000, "build/zephyr/zephyr.bin")], compress=True)
#   ^ note C5 flashes at 0x2000 not 0x0 (memory: project_c5_flash_and_bt);
#     offset comes from west runners.yaml as today.

# 4. post-flash reset that actually boots the app:
reset_chip(esp, "jtag-reset")      # C6: ndmreset over USJ vendor iface

# 5. close
esp._port.close()
```

**Caveat — port path vs USB device.** `detect_chip` takes a *serial* path
(`/dev/serial/by-id/...-if00`, the CDC). The JTAG reset needs the *USB device*
(to claim interface 0xFF). esptool's `get_usb_vid_pid()` resolves VID/PID from the
serial path via pyserial (`loader.py:689-727`) but never opens the raw USB device.
`JTAGSystemReset` must map `esp._port` → the USB device. Two ways:
- derive the sysfs USB topology path from the tty (esp32-zephyr already does this:
  `scripts/dev/discover.py:28-45` `find_tty_for_usb` / `read_sysfs_vid_pid`, and
  `espjtag`'s `usb_path` matcher `transport.py:21-38` takes a `"bus-ports"`
  string); pass that to `EspUsbJtagTransport(usb_path=...)` so the right unit is
  picked on a multi-device bench;
- or match by USB serial-number string (each unit's `iSerial`).
**This matters on our bench** — many `303a:1001` on one hub
(`flash.py:399`, `count_303a_1001_devices()`); the JTAG reset MUST be pinned to
the same physical unit esptool flashed, exactly like OpenOCD's
`adapter usb location` pin (`flash.py:400`, `C6-USJ-RESET.md:44-46`).

For our internal `dev.py`, the simplest correct integration is: keep using the
vendored `espjtag` directly for the reset (we already have `vendor/espjtag`,
`flash.py:376`) and only switch the *flash* to the in-process esptool API — i.e.
we don't strictly need esptool to own the JTAG reset for our own tooling. The
esptool-owned `JTAGSystemReset` matters for the **upstream PR** and for anyone
using esptool's CLI.

---

## 5. Device / interface sharing: CDC + JTAG vendor interface coexistence

**The key feasibility question, and the answer is favourable.**

A `303a:1001` ESP32-C6 exposes **one USB device with multiple interfaces**:
- interfaces 0/1 = CDC-ACM (classes 0x02/0x0a) — the serial console esptool drives
  (`espjtag/transport.py:42-43`, `constants.py:7-8`);
- interface **0xFF** (vendor-specific) = the esp_usb_jtag bulk JTAG interface
  (`transport.py:44-48`, found dynamically via `bInterfaceClass=VENDOR_CLASS`).

USB allows different host drivers to claim **different interfaces of the same
device simultaneously**. So in principle: the kernel `cdc-acm` driver owns
iface 0/1 (esptool's `/dev/ttyACM*`), and pyusb claims **only** iface 0xFF for the
JTAG reset (`transport.py:49-54`: `detach_kernel_driver(self.iface)` +
`claim_interface(dev, self.iface)` — note it detaches/claims *only the JTAG
iface*, never the CDC). **They coexist.** This is exactly what OpenOCD does today
on our bench while a console is open, and what `espjtag` does (it never touches
the CDC).

Therefore the design is: **esptool keeps the CDC port open** the whole time;
`JTAGSystemReset` opens a *separate* pyusb handle to the *same device's* 0xFF
interface, pulses ndmreset, and disposes (`espjtag/reset.py:45`
`usb.util.dispose_resources`). No need for esptool to release the serial port
first.

**Caveats / things to verify on the bench:**
1. **`__call__` reopen loop.** `ResetStrategy.__call__` (`reset.py:45-64`) reopens
   `self.port` (the CDC) if closed and only catches `OSError`. A JTAG reset that
   raises a `usb.core.USBError` won't be caught there → either override
   `__call__` in `JTAGSystemReset` or wrap the USB work so it doesn't rely on the
   serial-port retry semantics. Minor.
2. **Post-reset CDC re-enumeration.** When ndmreset fires, the chip reboots and
   the **USB device re-enumerates** — the CDC `/dev/ttyACM*` drops and comes back
   (the same drop esptool already handles for USJ resets, hence the retry loop at
   `reset.py:44-64`). After `jtag-reset`, esptool's `teardown()` then closes
   `esp._port` (`__init__.py:644-645`); if the port already vanished, the close
   may error — guard it. The OpenOCD path sees the same cosmetic `LIBUSB_ERROR_IO`
   (`C6-USJ-RESET.md:50-51`).
3. **Who detaches the CDC?** We must NOT detach the cdc-acm driver from iface 0/1
   (that would kill esptool's port). `espjtag` correctly detaches only the JTAG
   iface (`transport.py:49-54`) — preserve that invariant in the vendored copy.
4. **udev permissions.** Raw pyusb access to `303a:1001` needs a udev rule or
   root (espjtag README "Install"); esptool's serial access does not. The PR docs
   must mention this, and `JTAGSystemReset` should give a clear error if it can't
   claim the interface.

**Verdict:** CDC + JTAG-vendor-interface coexistence on the same `303a:1001` is
**feasible and is the intended USB design** — no release/handoff dance needed. The
only real risk is the post-reset re-enumeration timing (already a known,
handled-elsewhere quirk), not interface contention.

---

## 6. PR-acceptance test matrix

Legend — **Bench**: ✅ we can test (C5, C6 ×2 incl. UM TinyC6, S3 variants per
`scripts/esp32-devices.json` / memory); ⚙️ needs hardware we don't have; n/a not
applicable.

### 6a. Connection-capability reference (which chips have what)

| Chip | Arch | Built-in USJ (303a:1001)? | USB-OTG (303a:1002)? | RISC-V Debug Module (ndmreset)? | JTAG-reset applies? |
|------|------|---------------------------|----------------------|--------------------------------|---------------------|
| ESP32 | Xtensa | no (UART only) | no | no | no — needs UART bridge, RTS works |
| ESP8266 | L106 | no | no | no | no |
| ESP32-S2 | Xtensa | yes | yes | no (Xtensa OCD) | no |
| ESP32-S3 | Xtensa | yes | yes | no (Xtensa OCD) | no |
| ESP32-P4 | RISC-V | yes | yes | **yes** | yes (verify) |
| ESP32-C2 | RISC-V | **no** (UART only) | no | n/a over USJ | no |
| ESP32-C3 | RISC-V | yes | no | **yes** | **yes** |
| ESP32-C5 | RISC-V | yes | no | **yes** | yes (but watchdog already works) |
| ESP32-C6 | RISC-V | yes | no | **yes** | **yes — primary target** |
| ESP32-H2 | RISC-V | yes | no | **yes** | yes (verify idcode/DM) |

Key facts: only the listed parts have a built-in USJ; Xtensa USJ parts (S2/S3)
share the *transport* but use Xtensa OCD, not the RISC-V Debug Module, so
ndmreset/dmcontrol don't apply (`espjtag/constants.py:20-24`, README.md:71-73) —
and S3 boots fine via esptool anyway. C2 has no USJ at all (UART bridge only).
The Xtensa parts must continue to use their existing reset (regression guard).

### 6b. The (chip × connection × before × after) matrix

| # | Chip | Connection | --before | --after | Expected behaviour | Why | Bench |
|---|------|-----------|----------|---------|--------------------|-----|-------|
| 1 | C6 | USJ 303a:1001 | usb-reset / no-reset | **jtag-reset** | flash OK → **app RUNS** | the fix: ndmreset re-straps (#970) | ✅ (gate test) |
| 2 | C6 | USJ | no-reset | hard-reset | flash OK → **stranded in ROM** (today's bug) | RTS no-op over USJ — baseline to prove the fix matters | ✅ |
| 3 | C6 | USJ | no-reset | watchdog-reset | warns, falls back to no-op hard-reset → stranded | watchdog disabled on C6 (`esp32c6.py:208-211`) | ✅ |
| 4 | C6 | external CP210x/FTDI UART | default-reset | jtag-reset | **falls back to RTS hard-reset**, app runs | no JTAG iface on a UART bridge → graceful fallback (§2c else-branch) | ✅ (if we wire a bridge to C6 UART0) |
| 5 | C5 | USJ | usb-reset | watchdog-reset | flash OK → app runs (unchanged) | C5 watchdog path already works (`esp32c5.py:225-227`) — **must not regress** | ✅ |
| 6 | C5 | USJ | usb-reset | jtag-reset | flash OK → app runs | C5 also has the DM; new path should also work | ✅ |
| 7 | C3 | USJ | usb-reset | jtag-reset | flash OK → app runs | same DM as C6; covered by shared override | ⚙️ (have C3 AC:27 — memory) ✅ if used |
| 8 | H2 | USJ | usb-reset | jtag-reset | flash OK → app runs | RISC-V DM; **verify idcode/DM quirks** | ⚙️ |
| 9 | S3 | USJ 303a:1001 | usb-reset | hard-reset | flash OK → app runs (unchanged) | Xtensa, no RISC-V DM; jtag-reset must **not** be selected/offered as the booting path | ✅ |
| 10 | S3 | USB-OTG 303a:1002 | usb-reset | hard-reset/watchdog | unchanged S3 OTG path (`esp32s3.py:356-368`) | OTG strap logic already handled; JTAG reset n/a | ✅ (S3 OTG, see memory project_s3_usb_otg_vs_jtag) |
| 11 | S2 | USJ / OTG | usb-reset | hard-reset | unchanged | Xtensa | ⚙️ |
| 12 | ESP32 | CP2102 UART (real RTS) | default-reset | hard-reset | unchanged classic reset | regression: bridge with real RTS must be untouched | ⚙️ (no plain ESP32 on bench?) |
| 13 | ESP32 | CP2102C (HW flow ctrl) | default-reset | hard-reset | unchanged, flow_control path (`loader.py:821`) | regression for the flow-control adapter branch | ⚙️ |
| 14 | C2 | UART bridge | default-reset | jtag-reset | **falls back to hard-reset** (no USJ) | C2 has no USJ; must degrade, not error | ⚙️ |
| 15 | P4 | USJ / OTG | usb-reset | jtag-reset | flash OK → app runs (verify) | P4 is RISC-V with a DM; likely works but unverified | ⚙️ |
| 16 | C6 | USJ | no-reset | no-reset | stays in ROM (no reset requested) | sanity: jtag-reset only fires when asked | ✅ |
| 17 | C6 | USJ, multi-device bus | no-reset | jtag-reset | reset hits the **correct** unit | USB-path/serial pinning (§4a, `flash.py:399`) — wrong-tap regression | ✅ (we have 2× C6) |

### 6c. The end-to-end acceptance test (the only one that proves the fix)
Flash a real app + run `--after jtag-reset` and **observe boot output on the
console** (not just "esptool exited 0"). This is row 1, and it is the
go/no-go for the whole PR. esp32-zephyr already has the harness:
`_stream_boot` (`scripts/dev/flash.py:354-355`) and the
"flash+boot+console" test (commit `8e589d9`). Reuse it. Acceptance = the C6
prints its banner after `jtag-reset` with **no OpenOCD invoked**.

### 6d. Is this all the scenarios? — completeness check
Yes, the axes are: {arch: Xtensa/RISC-V} × {connection: USJ / USB-OTG / external
UART (real-RTS, HW-flow-ctrl)} × {has-RISC-V-DM} × {before} × {after incl. the new
jtag-reset} × {single vs multi-device bus} × {fallback when no JTAG}. The table
covers each materially-distinct combination. The four that matter most:
- **row 1** (the fix works — C6 boots),
- **row 4/14** (graceful fallback when no JTAG iface — UART bridge, C2),
- **rows 5/9/12/13** (no regression where esptool already works — C5 watchdog,
  Xtensa, real-RTS bridges),
- **row 17** (correct device on a multi-303a:1001 bus).
Anything not in the table is either a duplicate axis or out of scope (flash-over-
JTAG, the full debugger).

---

## 7. Open risks / unknowns

1. **(Highest) Does pure-Python ndmreset actually boot from post-flash ROM?**
   §0. The repos contradict each other. **Action item 0** resolves it. If "no"
   (branch B), the `JTAGSystemReset` body must carry OpenOCD's deeper
   examine/halt/ack/resume sequence (more than `espjtag.transport` + the current 7
   writes), or the C6 post-flash boot stays on OpenOCD and the PR shrinks to
   "reboot a *running* C6 via `--after jtag-reset`."
2. **pyusb as an esptool dependency.** Upstream may resist. Mitigate: lazy import
   inside `JTAGSystemReset.reset()`, graceful fallback if missing, document the
   udev rule. Possibly propose it as an `esptool[jtag]` extra.
3. **Device pinning on a multi-303a:1001 bus.** The JTAG reset MUST target the
   same physical unit esptool flashed (row 17). Mapping `esp._port` (a tty) → the
   USB device is non-trivial; reuse `discover.py:28-95` topology logic and
   `espjtag`'s `usb_path` matcher (`transport.py:21-38`). Getting this wrong =
   "Unsupported DTM version / wrong tap" (`C6-USJ-RESET.md:44-46`).
4. **Post-reset CDC re-enumeration & port close.** ndmreset re-enumerates USB; the
   CDC drops. `teardown()`'s `esp._port.close()` (`__init__.py:644-645`) may hit a
   vanished port — guard. The `ResetStrategy.__call__` retry loop
   (`reset.py:45-64`) only catches `OSError`, not `USBError` — handle in the new
   class (§5 caveat 1).
5. **GPL/Apache provenance.** espjtag is Apache-2.0 but ported from GPL-2.0
   openocd-esp32; esptool is GPL-2.0-or-later. Vendor the transport with the
   GPL header + openocd-esp32 attribution (§3 licensing note).
6. **H2 / P4 / C2 unverified.** H2 and P4 DM behaviour and C2's no-USJ fallback
   (rows 8, 14, 15) aren't on our bench. The fallback path (no JTAG → hard-reset)
   must be defensive so an untested chip degrades, never errors.
7. **Auto-selection scope creep.** Tempting to make the C6 silently use jtag-reset
   for `--after hard-reset` (§2d). Keep it opt-in for the upstream PR; only our
   fork should consider defaulting it.
8. **Stub vs ROM context.** Verify the ndmreset works the same whether esptool is
   talking to the ROM loader or the running stub (the stub also runs on the core
   that ndmreset resets — should be fine, it's a full-system reset, but confirm in
   row 1 with `run_stub` in the loop).

---

## 8. Summary of concrete edit points (for the implementer)

| Concern | File:line in fork | Action |
|---------|-------------------|--------|
| New reset class | `esptool/reset_jtag.py` *(new)* + `esptool/reset.py:105` | add `JTAGSystemReset` (vendored esp_usb_jtag transport + ndmreset) |
| `--after` CLI choice | `esptool/__init__.py:405-407` | add `"jtag-reset"` |
| `--after` dispatch | `esptool/cmds.py:2416-2453` | add `elif "jtag-reset": esp.jtag_system_reset()` |
| Base loader method | `esptool/loader.py:2090` (next to `watchdog_reset`) | add `jtag_system_reset()` warn+fallback |
| C6/C3/C5/H2 override | `esptool/targets/esp32c3.py:15` (covers all via inheritance) | `jtag_system_reset()` guarded by `uses_usb_jtag_serial()` (`loader.py:1241-1245`) |
| Library flash+reset | n/a (caller side, esp32-zephyr `scripts/dev/flash.py`) | replace `_esptool()` subprocess with `detect_chip`/`attach_flash`/`write_flash`/`reset_chip` (§4a) |

The integration mechanics are low-risk and small. **The whole plan hinges on
Action item 0** (§0): prove the pure-Python ndmreset boots a C6 out of post-flash
ROM-download on the bench. Run that test first.

---

## BENCH FINDINGS (2026-06-10, verified on hardware)

### Action Item 0 RESOLVED: pure-Python ndmreset does NOT boot a C6 from post-flash ROM
Decisive test on xiao-c6-b: flashed `--after no-reset` (left in ROM, console
dead) → ran `espjtag.reset.reset_run()` (`reset_run -> True`) → console STILL
dead. The bare ndmreset pulse does not boot from ROM. So:

> **Update:** the deeper recipe was subsequently reproduced in pure Python as
> `espjtag.reset_run_from_rom()` — a USB bus reset *plus* the soc-reset /
> halt / dcsr / resume handshake — which **is** bench-proven 3/3 from post-flash ROM
> (see DEVICES-AND-FLASHING.md / C6-USJ-RESET.md). So OpenOCD is no longer the *only*
> path; `dev.py` still delegates to it as the established path pending the switch-over.
> The point below stands: the reset *must carry the deeper sequence*, which
> `reset_run()` alone does not.

- espjtag's `reset_run()` docstring claim ("re-straps and boots the app") is
  **wrong for the post-flash ROM case** — it only cleanly reboots a RUNNING core.
  (Filed as espjtag #2; README is correct.)
- **The minimal esptool JTAG-reset PR cannot be just the ndmreset pulse** — it must
  replicate OpenOCD's full reset-run (assert ndmreset + examine + set dcsr + halt
  → resume). Until espjtag #2 lands, the esptool integration's post-flash boot
  must call OpenOCD-equivalent logic, not the bare ndmreset. This raises the bar
  for the PR (more than a few lines) and must be flagged honestly upstream.

### DESIGN PRINCIPLE: one device handle + its sub-interface map, threaded through dev
The 303a:1001 device exposes MULTIPLE interfaces: iface 0/1 = CDC-ACM console
(0x02/0x0a), iface 2 = vendor-spec JTAG (0xFF). They are separate interfaces of
ONE physical device, so a single process can hold BOTH at once (esptool on the
CDC, the JTAG reset on the vendor interface) with no release/handoff.

This is the core argument for the in-process (library) path over subprocess:
- A subprocess hands the port back between flash and reset; a second process can
  grab/wedge it in the gap (the contention class behind the historical
  write-timeout/zombie pileups).
- The library path opens the device ONCE, enumerates its interfaces once, and
  threads that handle (device + {cdc, jtag} interface map) through flash → reset →
  console — no re-discovery, no inter-process race.

So dev.py should pass around a *device handle object* (the opened pyusb device +
its discovered sub-interface map), not a tty path or usb-path string that each
step re-resolves. This is the "passing it around in dev" design: discover once,
own the whole device, use the right interface per action.

### Consequence: support BOTH subprocess and library call paths
To test the esptool fork mods for an upstream PR we need CLI-compatible
(subprocess) behaviour AND the in-process library path. Keep both, cleanly: the
library path is the bench default (one process owns the device across
flash+reset); the subprocess path mirrors the CLI for PR test-suite parity and
surfaces exactly where subprocess port-handoff costs us.
