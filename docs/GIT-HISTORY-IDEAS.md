# Git-history archaeology — alternative ideas for espjtag / esptool-fork / the GUI

Mining the user's local repos (and relevant upstream history) for **prior art,
abandoned approaches, and alternative ideas** that bear on the current work:
`espjtag` (pure-Python RISC-V JTAG over the ESP32 USB-Serial/JTAG), the esptool
fork, and the Flutter debug GUI.

This is archaeology, not a design doc. For the actual designs see the sibling
docs (which this complements, doesn't duplicate):

- the JTAG transport speed plan (drain/batch/async) — perf issues under
  [espjtag#8](https://github.com/awtoau/espjtag/issues/8).
- `ESPTOOL-JTAG-INTEGRATION-PLAN.md` — wiring the ndmreset into esptool.
- `FLUTTER-DEBUG-GUI-DESIGN.md` — the OpenOCD-Tcl-RPC GUI architecture.
- `OTHER-CORE-DEBUG-PROTOCOLS.md`, `HARDWARE-JTAG-VS-USB-JTAG.md`.

Every path is absolute; every claim cites a file or a commit SHA. Where a repo's
own README/commit already states the rationale, it is quoted rather than guessed.

---

## 1. The Rust USB samples — cross-platform USB (#13) + USB reset/power (#6)

The user has **four** distinct Rust codebases touching USB device handling, all
using `rusb` (libusb). Ranked by how directly each informs espjtag's
cross-platform USB layer:

### 1a. `/home/dan/git/awto-usb` — **the best basis for #13 (cross-platform USB)**
Single commit `6c2fde3` "Initial: Rust USB enumeration + Flutter tree
visualization". This is the cleanest, smallest, most on-point sample.

- `/home/dan/git/awto-usb/src/usb.rs` — `list_devices()` (lines 168-213) is a
  textbook `rusb` enumeration: opens each device best-effort, reads
  manufacturer/product/serial string descriptors, captures `bus`, `address`,
  `port_numbers` (the sysfs port chain), `class_code`, and link speed
  (`speed_label`, lines 154-163, Low→SuperPlus). **This `port_numbers` vector is
  exactly espjtag's `usb_path` matching key** (`espjtag/transport.py:51-64`
  matches a `"1-1.3.1.3.1"` string against `d.bus` + `tuple(d.port_numbers)`).
  awto-usb already has the typed Rust equivalent.
- `build_tree_json()` (lines 1-99) builds a bus→hub→hub→device tree from the port
  paths, with class-code→icon mapping (0x09 HUB, 0x03 HID, 0x02/0x0a CDC,
  0xFF+product-string heuristics for STLink/Espressif). **Reusable for a GUI
  "which port is my board on" picker** — the espjtag/GUI problem of pinning one
  of N `303a:1001` units on the bus.
- Pairs with a Flutter app (`/home/dan/git/awto-usb/flutter_app/`,
  `FLUTTER_MIGRATION.md`) — a worked Rust-core + Flutter-tree pattern.
- **Why it's the best basis:** no debugger logic, no STLink baggage — just
  "enumerate, resolve a stable port path, render a tree." That's precisely the
  cross-platform substrate espjtag #13 needs (Windows/macOS where `/sys/bus/usb`
  doesn't exist; `port_numbers` works on all three via libusb).

### 1b. `/home/dan/git/awto-flash` — the Transport-trait + parallel-scan pattern
A substantial `rusb` STLink/ST-probe tool (~2,400 LoC of Rust). Most relevant
files:

- `/home/dan/git/awto-flash/src/awto_usb.rs` — a `Transport` **trait** (lines
  11-15: `cmd`/`write`/`read`) with a `BulkChannel` impl over libusb bulk
  endpoints, a `TracingTransport` decorator that counts calls/bytes/errors into
  atomics (lines 135-191), and `flush_in()` (lines 38-41) — **the same
  "drain stale IN bytes" idea espjtag agonised over** (`espjtag/transport.py`
  `_drain_in`), here a 5 ms-window read loop. The trait+decorator shape is a good
  model if espjtag ever grows a Rust core or wants pluggable transports
  (real/sim).
- `/home/dan/git/awto-flash/src/awto_probe.rs` — `Probe` struct carries
  `sysfs_path: Option<String>` (line 31), `usb_speed` + `usb_speed_degraded`
  (lines 25-27: flags a probe enumerating below its expected speed = bad
  cable/hub), and `vcp_ports` (the serial ports a probe exposes). **The
  USB-speed-degraded check is a nice diagnostic** — espjtag/GUI could warn when a
  `303a:1001` came up Full-Speed-on-a-bad-hub.
- `list()` (lines 135-178) scans all probes **in parallel** (one thread per
  device, `thread::spawn` + join), with a `--sequential` debug fallback. This is
  the same parallel-by-device pattern as `dev.py fleet` and `awto-serial`'s
  `discover`.
- It also has a `SimTransport` (the `Transport` trait makes hardware optional) —
  worth copying for espjtag CI that has no board attached.

### 1c. `/home/dan/git/can-test-harness` — vendor-class control transfers + DFU + power
- `/home/dan/git/can-test-harness/src/gs_usb.rs` — opens by VID/PID, **detaches
  the kernel driver** and issues **vendor-class control transfers**
  (`request_type(Direction::Out, RequestType::Vendor, Recipient::Interface)`,
  lines 26-45). espjtag does the analogous thing in Python
  (`detach_kernel_driver` + `ctrl_transfer(0x40, 0, 20, …)` for VEND_JTAG_SETDIV,
  `transport.py:79-94`). The Rust idioms here are the reference if porting that.
- `/home/dan/git/can-test-harness/src/power.rs` — a `PowerBackend` **trait**
  (lines 8-19) with a `cycle(off, wait, on)` default method and a `Uhubctl` impl
  shelling to `uhubctl -l <loc> -p <port>`. The doc comment explicitly says "swap
  in the Rust VBUS crate via the trait" — i.e. the abstraction is there, the
  direct-libusb backend was a TODO. **esp32-zephyr already built that
  direct-libusb backend in Python** (see §2b) — the Rust side never got it.
- `/home/dan/git/can-test-harness/src/dfu.rs` — DFU detach/poll with a bounded
  retry loop (200 ms poll, capped) — a model for "wait for re-enumeration after a
  reset" without a blind sleep.

### 1d. `/home/dan/git/apollo` (LUNA/Cynthion) — a **second** pure-Python JTAG TAP impl
- `/home/dan/git/apollo/apollo_fpga/jtag.py` (31 KB) + `onboard_jtag.py` — a
  full pure-Python JTAG TAP state machine for the ECP5/ILA, **independent of
  espjtag's** (`espjtag/transport.py`'s `_NEXT` graph + `_goto` BFS). Worth a
  cross-read: if espjtag's state-machine ever has an edge-case bug, apollo is a
  second independent implementation of the same 16-state graph to diff against.
  (Different transport — FPGA, not esp_usb_jtag — but the TAP layer is the same
  IEEE 1149.1 machine.)
- `/home/dan/git/cynthion-workspace/debris/awto-cynthion/firmware/smolusb/` — a
  from-scratch Rust USB **device** stack (descriptors, control, ACM class). Not
  host-side, but the descriptor/control-transfer modelling is a reference for the
  OTG dual-CDC firmware work (the current branch is `feat/usb-otg-dual-cdc`).

**Shortlist for #13:** start from **`awto-usb`** (`src/usb.rs`) for the
enumeration + port-path + tree, lift the **`Transport`/`TracingTransport` trait
shape from `awto-flash`** if a pluggable transport is wanted, and the
**vendor-class control-transfer idiom from `can-test-harness/gs_usb.rs`**.

---

## 2. esp32-zephyr history — USB reset/power evolution + abandoned approaches

The reset/recovery story in `/home/dan/git/esp32-zephyr` evolved through several
approaches; the dead ends are as informative as the survivors.

### 2a. The DAPLink / RP2350 probe was a **phantom** — do not chase it
The brief flagged "it mentioned an RP2350 DAPLink earlier." The history shows
that note was **wrong and was explicitly retracted**:

- `66b069c` brought the "AWTO ESP Debugger (Debug Mate)" across as a board.
- `8e0bea0` "drop the wrong DAPLink note": *"removed the WRONG DAPLink usb_id
  (it's just an ESP32-S3, no RP2350/DAPLink — that was a stale/incorrect note)…
  usb_ids is now the real S3 USB-Serial/JTAG app/ROM."*

So **there is no RP2350 DAPLink prior art to revisit** — the Debug Mate is a plain
ESP32-S3 that itself uses the built-in USB-Serial/JTAG, the same transport espjtag
already targets. Don't reintroduce a DAPLink transport on its account.

### 2b. USB power: uhubctl → **direct-libusb hub control** (kept; ~30× faster)
- `8148ea1` "Switch USB hub power to direct libusb, loop bootloader": replaced the
  `uhubctl` subprocess with **direct libusb hub-class control transfers**
  (`SetPortFeature`/`ClearPortFeature` `PORT_POWER`). *"~30× faster (off+on ~30ms
  vs uhubctl ~5s) and runs without sudo once the matching udev rule grants
  plugdev rw on the hub."* Added an **atexit + SIGINT/SIGTERM/SIGHUP handler that
  always re-powers any port left off** (the MEMORY.md `feedback_usb_power_safety`
  rule, now codified).
- Lives now in `/home/dan/git/esp32-zephyr/scripts/dev/power.py:61-112`
  (`_set_port_power`, `power_off_then_on`). **This is the production pattern the
  Rust `can-test-harness/power.rs` left as a TODO** (§1c). If a Rust GUI ever
  wants port power, port this Python directly.
- The deleted `boot-to-rom.py` (`8148ea1` body) is a cautionary tale already in
  MEMORY.md (`feedback_check_existing_wrappers`): it duplicated dev.py's
  bootloader, *"missing the esptool sync verification (could falsely report 'ROM'
  on a hwcdc-app spoofing 303a:1001)."* Lesson for espjtag/esptool integration:
  **verify the chip is actually in the mode you think via a protocol handshake,
  not just by VID:PID**, which can be spoofed by a running app.

### 2c. USB reset: USBDEVFS_RESET re-enumeration (kept; the no-unplug recovery)
- `4752a95` "feat(dev.py): usb-reset — USBDEVFS_RESET re-enumeration, no
  unplug/VBUS-cut". Implementation in
  `/home/dan/git/esp32-zephyr/scripts/dev/power.py:186-235`: resolve the device's
  `/dev/bus/usb/BBB/DDD` node from sysfs `busnum`/`devnum`, then
  `fcntl.ioctl(fd, USBDEVFS_RESET)`. **This is the software recovery for a wedged
  USJ endpoint when the bench hub can't cut VBUS** (the C5/C6 case). espjtag's
  own ROM-boot relies on exactly this (§3b).
- The `USBDEVFS_RESET = (ord('U') << 8) | 20` constant + sysfs busnum/devnum
  resolution (`power.py:198-216`) is the **Linux** path. For #6/#13
  cross-platform, the equivalent is libusb `reset_device()` (works on
  Windows/macOS too) — a reason to route USB reset through libusb (pyusb's
  `dev.reset()`) rather than the raw ioctl, so it's portable.

### 2d. udev symlinks — **tried and dropped** (#25), don't resurrect
- `d98da9d` "Remove udev symlink approach for device ID": dropped
  `72-awto-esp.rules` + `gen-udev-esp.py`. Decision: *"Host-side USB ID stays as
  the stock /dev/serial/by-id path keyed on the factory MAC; no extra udev
  layer."* Aligns with MEMORY.md `feedback_use_by_id_not_ttyacm`. So the device-ID
  story is settled: **by-id path, not a custom udev symlink, not ttyACM<N>.**

### 2e. The C5 "Write timeout" root cause — mechanism worth carrying into esptool
- `4b8018a` "research: C5 flash 'Write timeout' root cause = USJ blocks writes
  while app holds port": the USJ **blocks host writes until the on-chip app reads
  the OUT FIFO** (esp-idf #8670). While the Zephyr CDC console owns USB, esptool's
  reset/SYNC write never drains. A USJ reset is only a core reset (doesn't
  re-sample strapping, esptool #970), so the app keeps the port. **This is the
  documented mechanism behind all native-USB-JTAG flash flakiness** and the reason
  `reboot_bl` (firmware releases USB) is the right primary path, JTAG/USB-reset
  the fallback. Relevant to esptool-fork: the fork should prefer firmware-side
  download entry, and fall back to the espjtag ndmreset + USBDEVFS_RESET combo.

### 2f. The JTAG path itself — `dev.py` vendored espjtag, then measured it vs OpenOCD
The esp32-zephyr JTAG commits trace espjtag's adoption:
- `69e9e12` (first pure-Python JTAG client, standalone `esp_usb_jtag.py`) →
  `dd9491d` (vendored espjtag as a submodule) → `4569212` (deleted the standalone
  copy, deduped to the vendored package).
- `20977e2` built an **OpenOCD comparison into the harness**: *"espjtag is
  ~3.6ms/word; OpenOCD ~0.74ms/word because it BATCHES 32 reads into one
  transaction. That ~5× gap is the scan-batching lever."* This is the
  quantified motivation behind espjtag's later `_dmi_batch` work (§3c). The harness
  (`scripts/jtag_testdb.py`, adapter-aware) is the measurement substrate for any
  future espjtag perf idea — **don't build a new benchmark, extend that one.**

---

## 3. espjtag history — assumptions made and corrected (the sagas)

`/home/dan/git/espjtag` is young but dense; the commit messages are the design
record. The valuable archaeology is the **assumptions that were wrong and got
fixed** — each is a place where the "obvious" approach was a trap.

### 3a. The drain saga — a defensive drain that was pure latency tax
`espjtag/transport.py:106-173` (and commits `1a188b4` "drop _drain_in timeout
20ms->1ms ~6x", then `33dc131` "remove the unnecessary IN-drain ~88x on single
ops"). The arc:
- The original code drained the IN endpoint before every op "to clear stale
  bytes." But the endpoint is almost always empty, so the read **waited the full
  timeout every time** (~20 ms/op; even at a 1 ms request the kernel floored it to
  ~3 ms on this Full-Speed device).
- Insight (`transport.py:113-117`): `_recv` reads **exactly** the captured byte
  count, so the endpoint is already empty afterward — **the drain was defending
  against a bug that precise byte-accounting already prevents.**
- Resolution: `drain_mode = "off" | "validate" | "always"`. Default `"validate"`
  drains-as-an-assertion at a backing-off interval (1→4→16→…→256), so a
  byte-accounting bug surfaces loudly **without** paying ~3 ms/op. **Pattern worth
  reusing anywhere a "defensive flush" is suspected: turn it into a sampled
  assertion, not an unconditional cost.**

### 3b. The ROM-boot discovery — ndmreset alone is **not** enough (3b067b9)
`3b067b9` "reset_run_from_rom() boots the C6 out of post-flash USJ ROM (3/3)" and
`debug.py:280`. The corrected assumption:
- A bare `ndmreset` does **not** boot a freshly-flashed C6 out of USJ ROM download
  mode (verified 0/3; even OpenOCD's pure-JTAG `reset run` is 0/3). The USJ can
  only trigger a **core** reset, which does not re-sample the BOOT strap.
- A USB **bus** reset (USBDEVFS_RESET) alone is also 0/3 (core still parked in
  esptool's download stub).
- **The combination, in order, is 3/3 (6/6 total):** (1) USB bus reset →
  re-enumerate the USJ, clear the download latch; (2) ndmreset + the full OpenOCD
  reset-run handshake (soc_reset SBA writes, deassert_reset, halt_go,
  set_dcsr_ebreak, resume_go) → the re-strapped ROM boots from flash.
- **Reconciled (the apparent contradiction was a `reset_run()` vs
  `reset_run_from_rom()` mix-up):** both statements are true. The *bare* ndmreset
  (`espjtag.reset.reset_run()`, and what `README.md` warns "⚠️ use OpenOCD" about)
  is 0/3 from post-flash ROM. The *combination* (`reset_run_from_rom()`: USB bus
  reset + the full handshake) is 3/3 — bench-proven. `dev.py`'s `c6_reset_run`
  **still delegates to OpenOCD** as the long-established proven path while the
  pure-Python `reset_run_from_rom()` switch-over is pending. So: pure Python *can*
  boot from ROM via `reset_run_from_rom`; it cannot via a bare `reset_run()`. The
  esptool JTAG-reset class must therefore carry the *deeper* sequence, not just the
  pulse (ESPTOOL-JTAG-INTEGRATION-PLAN.md §0 / Action Item 0, resolved). See
  [C6-USJ-RESET.md](C6-USJ-RESET.md) and
  [ESPJTAG-STORY.md](ESPJTAG-STORY.md) for the canonical statement.

### 3c. The batching win — TAP-stays-in-DMI across DR scans (the #8 lever)
`espjtag/transport.py:429-492` (`_dmi_batch`) and `debug.py:126-167`
(`read_mem`), commits `9e225e6`/`28e95d2` (~38×, "matches OpenOCD"). The idea that
unlocked it: **the TAP can stay in IR=DMI across consecutive DR scans** (one
reset_tap + one IR-select for the whole batch), and the SBA
readonaddr+readondata+autoincrement hardware walks memory itself — so N word-reads
become a **pipeline** of DR scans in a handful of OUT/IN exchanges, not N
round-trips. Two pipelines stack (the DTM read-returns-previous pipeline + the SBA
readondata pipeline) and both are accounted for (`debug.py:136-155`). The
IN-FIFO back-pressure limit (`FIFO_CHUNK_BITS = 480 < 536`) is ported from
esp_usb_jtag.c's buffer math. **This is the headline perf idea and it's done; the
remaining lever (per the perf tracker [espjtag#8](https://github.com/awtoau/espjtag/issues/8))
is async/overlapped round-trips.**

### 3d. The C5 two-TAP discovery (22e738e) — single-TAP code "worked by luck"
`22e738e` + `transport.py:25-49`. The C5/C61 daisy-chain **two** irlen-5 TAPs (LP
core on tap0, HP RISC-V on tap1); C3/C6 expose one. The single-TAP code drove
every scan as if tap1 were the whole chain, so `read_idcode() == read_dtmcs() ==
0x00017c25` (it was reading tap0's IDCODE register for everything). The fix made
the chain layout explicit (`taps_after`/`taps_before`/`idcode_index`,
auto-detected from the IDCODE chain) **without disturbing the C6 path**. Lesson
for adding new chips (P4, future parts): **never assume single-TAP; interrogate
the IDCODE chain first.** `_CHAIN_BY_IDCODE` (line 47) is the extension point.

### 3e. No stray TODO/HACK markers
A scan of `espjtag/espjtag/*.py` for TODO/HACK/FIXME/"for now" found **none** — the
deferred work is tracked in commit messages, the README "What works" table
(`README.md:57-67`, flash-over-JTAG is the one 🚧), and GitHub issues (#8 batching
done, #13 cross-platform, #6 reset+power). The flash-over-JTAG roadmap item
(`README.md:67`: "ROM `esp_rom_spiflash_*` call") is the one genuinely-unbuilt
idea — a JTAG-driven flash path that would let espjtag flash without esptool at
all.

---

## 4. Prior-art patterns from the awto repos + the Debug Mate reference

### 4a. `/home/dan/git/awto-debug-embedded` — the **prior** multi-target debugger
This is the most directly-relevant prior art the brief asked about: an
**MCP-server multi-target embedded debugger** (`README.md`: "MCP server exposing
embedded debugger tools to AI agents"), predating espjtag. It took the
**subprocess-wrapping** approach espjtag deliberately replaced:

- `/home/dan/git/awto-debug-embedded/debugger_esp.py` — wraps `esptool.py` and
  `idf.py` as **subprocesses** (chip_info via `esptool --json chip_id`,
  write/erase/read_flash, `openocd_start` via `idf.py openocd` or standalone).
  espjtag's whole reason for existing (`espjtag/README.md:11-17`: "it replaced
  shelling out to OpenOCD") is the rejection of this approach for
  register/memory/reset. **But the subprocess wrappers are still useful** for the
  things espjtag does NOT do — flash write, build, monitor — so the GUI/esptool
  integration could keep these for flash while using espjtag for debug.
- `debugger_stlink.py` + `debugger_cube.py` + `cpu_registry.py`/`cpus.json` — a
  **target-CPU registry** with approval state. The "identify the chip, look it up
  in a JSON registry" pattern matches `esp32-chips.json` and is reusable for the
  GUI's chip-aware views.
- **The escalation ladder is the gold here.** Commits
  `daf58dc`/`ce5e747`/`93ae10f` built a 4-step recovery escalation:
  **UR (under-reset connect) → USB-reset → uhubctl VBUS power-cycle → ask the
  user**. `93ae10f` also persists a `target_uid` (on-chip 96-bit UID) and detects
  **sibling probes wired to the same target** (two STLinks on one board). For
  espjtag/GUI: an explicit, ordered recovery escalation (espjtag #6) with a
  "give up and tell the human" terminal step is a proven shape — and the
  same-target-UID dedup is how you avoid driving two probes into one chip.
- `7a7454a` "resolve ambiguous same-model ESP adapter port mapping": the
  **N-identical-303a:1001 problem**, solved. Changed the `(vid,pid)→serial` cache
  to `(vid,pid)→set[serial]` so it returns empty (forces a real per-port lookup)
  when multiple same-model adapters are present, and matches by serial first then
  VID:PID. **This is the exact disambiguation espjtag needs** when several ESP
  USB-JTAG units are on the bench — and it's already been worked out here.

### 4b. `/home/dan/git/awto-serial` — the device-discovery primitive
`2c81b04` "generic device-discovery primitive (discover) — identify-before-open,
parallel-by-port, baud + DTR probing." The **identify-before-open** principle
(probe what a device is before claiming it) and **parallel-by-port** scanning is
the same shape as `dev.py discover.py` and `awto-flash list()`. For a GUI device
picker, this is the reusable "safely enumerate candidates" primitive.

### 4c. `/home/dan/git/awto-blackmagic` — a real SWD/JTAG probe firmware fork
A Black Magic Probe fork (bmp-v2, F072 custom builds; `d632ae60`,
`76efdd0c` "STM32H750 halt and release usage"). Not USB-JTAG, but BMP is a
**GDB-server-on-the-probe** architecture — the alternative to espjtag's
host-drives-everything model. Worth knowing it exists as a contrast; the GUI doc
already chose OpenOCD Tcl RPC over GDB RSP, and BMP is the "GDB server" end of
that same trade-off.

### 4d. `dev.py` sysfs USB-path resolution — the Linux production path
`/home/dan/git/esp32-zephyr/scripts/dev/discover.py` is the mature version of
what espjtag does inline: `find_tty_for_usb` (sysfs path → ttyACM),
`_read_sysfs_serial`, `_hub_path_and_port` (split `"1-7.1"` into hub+port),
`count_303a_1001_devices` (the multi-unit enumerator), `discover_by_serial`.
espjtag's `transport.py:51-64` reimplements a slice of this (port-path matching)
against libusb instead of sysfs — the libusb version is the cross-platform one,
so **for #13 espjtag's approach is the right one to generalise**, not dev.py's
sysfs one (Linux-only).

---

## 5. The Flutter / GUI repos — reusable architecture

### 5a. `/home/dan/git/ChameleonUltraGUI` — the reference hardware-tool GUI
The standout reusable pattern is the **connector abstraction**:
`/home/dan/git/ChameleonUltraGUI/chameleonultragui/lib/connector/serial_abstract.dart`
defines `abstract class AbstractSerial` with `performConnect`/`performDisconnect`/
`write`/`registerCallback`/`availableChameleons`, and a `ConnectionType { none,
usb, ble }` enum. Concrete impls per platform/transport sit beside it:
`serial_native.dart`, `serial_android.dart`, `serial_macos.dart`,
`serial_mobile.dart`, `serial_ble.dart`, and crucially **`serial_emulator.dart`**
(a fake device for development without hardware). Above it,
`lib/bridge/chameleon.dart` is the **protocol layer** (command encoding) that
talks to whatever connector is plugged in, and `bridge/dfu.dart` is firmware
update.

**Directly reusable shape for the espjtag GUI:** one `AbstractSerial`-style
transport interface, per-platform impls, a `*_emulator` for hardware-free dev, and
a thin protocol/bridge above it. The espjtag GUI's "OpenOCD Tcl RPC vs pure-Python
espjtag backend" choice (`FLUTTER-DEBUG-GUI-DESIGN.md` §5, option B) is exactly
this connector-swap pattern — ChameleonUltraGUI proves it works for a real
hardware debug tool. **The emulator connector is the highest-value steal:** it's
how you build the GUI before the WiFi/USB plumbing is solid.

### 5b. `/home/dan/git/awto-usb/flutter_app` — Rust-core + Flutter-tree, already wired
The companion to §1a. A working example of a Rust USB enumerator feeding a Flutter
tree view (`FLUTTER_MIGRATION.md` documents the migration). If the GUI wants a
"USB topology / which port" panel, this is a ready template.

### 5c. `/home/dan/git/awto-gui-inspect-flutter` — dev-time inspection + safety classes
A Flutter package for **development-time GUI inspection + AI handoff + hardware-safe
command wiring** (`README.md`). Two ideas worth lifting for a *hardware* debug GUI:
- **Safety classes** — declare a UI action's safety requirement (motion,
  destructive) so the framework can gate it. For a JTAG GUI, "halt the core",
  "write memory", "reset" are exactly the destructive actions that want a gate.
  Commands are **metadata only**; actual hardware access goes through the project
  CLI/API — the same separation the GUI design doc wants (the phone never touches
  USB).
- **Stable debug IDs + AI logging** — every UI object gets a stable ID and can be
  shipped to an AI agent with a comment. Useful for the "AI-driven bench" angle.

### 5d. `/home/dan/git/awto-flutter-framework` — the Bloc/Cubit baseline + emulator philosophy
`ARCHITECTURE.md` lays out the house style: **Bloc** for complex async (the live
board-state poller is a Bloc with Loading/Loaded/Error states — `ARCHITECTURE.md`
"Error Handling": blocs emit error states, never throw), **Cubit** for simple
state, `Equatable` models, `bloc_test`. The espjtag GUI's `BoardState`
(`FLUTTER-DEBUG-GUI-DESIGN.md` §6) should be an `Equatable` model behind a Bloc
that owns the poll loop and emits `BoardStateLoaded`/`BoardStateError` — this repo
is the template for that.

---

## 6. Upstream OpenOCD / esptool history insights

### 6a. esptool USJ reset — the #970 limitation is fundamental, not a bug to fix
[esptool #970](https://github.com/espressif/esptool/issues/970) (ESPTOOL-842):
*"If originally booted in DOWNLOAD_MODE, the device stays stuck… rebooting does
not clear strapping pins. This only occurs with USB-Serial-JTAG, not UART."* The
sanctioned fix is `--after watchdog-reset` (full system reset → re-samples
strapping). The deeper note: *"the RTC WDT also resets the USB-serial-JTAG
device"* — i.e. **any reset strong enough to re-strap also drops the USB link**,
which is precisely why espjtag's ROM-boot (§3b) needs the USB **bus** reset +
re-enumerate step, not just a core reset. The current esptool-fork
(`/home/dan/git/esptool-fork`) already ships the canonical strategies:
`USBJTAGSerialReset` (DTR/RTS dance, `reset.py:145-163`) and `HardReset` with a
`uses_usb` branch (`reset.py:166-205`). **The espjtag ndmreset+USBDEVFS_RESET combo
is a strictly-better fourth strategy** to add as a new `ResetStrategy` subclass —
which is exactly what `ESPTOOL-JTAG-INTEGRATION-PLAN.md` proposes.

Note: `/home/dan/git/esptool-fork` is currently a **clean mirror of upstream**
(last commit `f6fded4`, the upstream C61 HMAC fix; remote `awtoau/esptool`). **No
awto-specific commits exist yet** — the JTAG-reset integration is greenfield, so
the minimal-boundary plan (one new ResetStrategy + a selection hook) is unobstructed.

### 6b. OpenOCD esp_usb_jtag perf — batching is upstream's lever too, plus async
- OpenOCD's `batching` config (0=none, 1/`wr`=batch writes [default], 2/`rw`=batch
  reads+writes) is the same scan-batching idea espjtag landed in `_dmi_batch` —
  confirming the approach is the mainstream one, not a hack.
  ([OpenOCD adapter config](https://openocd.org/doc/html/Debug-Adapter-Configuration.html))
- Espressif themselves treat the USJ adapter speed as **fuzzy** — the
  [questionable-adapter-speed issue (OCD-633) #248](https://github.com/espressif/openocd-esp32/issues/248)
  and the [slow JTAG flash issue (OCD-665) #259](https://github.com/espressif/openocd-esp32/issues/259)
  show the USJ is bandwidth-bound at the USB layer (Full-Speed, 12 Mbps), matching
  espjtag's own cost model (the ~1 ms FS frame is the
  floor). **Takeaway: TCK speed is a red herring; round-trip count is everything**
  — espjtag's batching + a future async/overlapped-IO path are the only levers,
  same as upstream.
  ([openocd-esp32 releases](https://github.com/espressif/openocd-esp32/releases))
- A "fast non-intrusive JTAG profile sampler (>1000× vs prior)" exists upstream
  for ESP32/S2 — not directly portable (Xtensa apptrace), but a hint that
  **sampling the PC over JTAG** is a feature worth having in the GUI for a "what's
  the core doing" view, done cheaply.

---

## 7. Revisit-these shortlist (ranked by value)

1. **Steal ChameleonUltraGUI's connector abstraction — especially the
   `serial_emulator` connector** (§5a). It's the single highest-leverage reuse:
   it lets the Flutter GUI be built and tested with zero hardware, and the
   OpenOCD-vs-espjtag backend swap the GUI design wants is literally a connector
   impl. Path:
   `/home/dan/git/ChameleonUltraGUI/chameleonultragui/lib/connector/`.

2. **Base espjtag's cross-platform USB (#13) on `awto-usb/src/usb.rs`** (§1a), and
   route USB **reset** through libusb `reset_device()` rather than the Linux-only
   USBDEVFS_RESET ioctl (§2c) so #6 is portable from day one.

3. **Port the 4-step recovery escalation from `awto-debug-embedded`** (§4a:
   under-reset → USB-reset → VBUS power-cycle → ask-human) into espjtag #6, and
   adopt its **same-model-adapter disambiguation** (`7a7454a`) for the
   N×`303a:1001` problem — it's already solved there.

4. **ROM-boot contradiction — RESOLVED** (§3b): both were true. The pure-Python
   `reset_run_from_rom()` (USB bus reset + full handshake) is 3/3; the bare
   `reset_run()` ndmreset is 0/3; `dev.py` keeps delegating to OpenOCD as the proven
   path with the pure-Python switch-over pending. The esptool JTAG-reset class must
   carry the deeper sequence, not the bare pulse (decided — see Action Item 0 in
   ESPTOOL-JTAG-INTEGRATION-PLAN.md). No longer blocks the integration.

5. **Build the JTAG-driven flash path** (`espjtag/README.md:67` roadmap, the only
   genuinely-unbuilt idea) via the ROM `esp_rom_spiflash_*` calls — it would let
   espjtag flash without esptool, closing the loop. Cross-reference the
   subprocess flash wrappers in `awto-debug-embedded/debugger_esp.py` for the
   fallback path while it's being built.

6. **Lift the `Transport`/`TracingTransport` trait + `SimTransport` shape from
   `awto-flash`** (§1b) if espjtag grows pluggable transports or a hardware-free
   CI — the tracing decorator (per-op byte/error counters) also feeds the perf
   harness directly.

7. **Adopt the "defensive flush → sampled assertion" pattern** from espjtag's own
   drain fix (§3a) anywhere else a precautionary USB read is suspected of being a
   latency tax — it converts a guaranteed cost into a cheap correctness check.

8. **Keep `awto-debug-embedded`'s subprocess wrappers for flash/build/monitor**
   (§4a) — espjtag should own debug (register/memory/reset), not reinvent flashing
   and building; those wrappers are the boring-but-correct path for the rest.
