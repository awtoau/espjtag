"""espjtag.chips — the single data-driven per-chip table (espjtag #4).

Everything chip-specific lives HERE, keyed by the target-TAP IDCODE, instead of
being scattered across transport.py / debug.py / constants.py. Adding a new ESP
part = one CHIPS entry, not edits in three files. The transport, the debugger, the
flash loader (#3), and the Xtensa port (#9) all look the chip up here.

Each entry carries:
  name        short label (C6, C5, S3, ...)
  core        "riscv" | "xtensa"  (which debug module — RISC-V DM vs Xtensa OCD)
  abits       DTMCS address-bit width (DMI scan width = abits + 34) [riscv].
              MEASURED live: C6 DTMCS=0x1071 -> abits 7; C5 DTMCS=0x70a1 -> abits 10.
  chain       TAP scan-chain layout (taps_after/taps_before/idcode_index) — the
              C5/C61 daisy-chain two TAPs; single-TAP parts use the {} default
  rom         per-chip ROM symbol addresses for the flash loader (#3) — the
              esp_rom_spiflash_* entry points + cache control (from <soc>.rom.ld)
  reset       SoC reset registers for reset_run_from_rom (the LP_AON SW-reset regs)
  flash_xip   the cache-mapped flash window base (SBA can't read it — use progbuf)
  sram        a scratch SRAM window {data, stack, trap} the flash loader stages
              args/buffer/return-trap into while the hart is HALTED (#3)

Values are MEASURED / sourced (cite in comments), not guessed. Unknown fields are
omitted rather than filled with a plausible-but-wrong number — a wrong ROM address
would crash the target. RISC-V parts are populated; Xtensa (#9) carries the
core type + IDCODE so it's recognised, with the debug-module specifics deferred to
the Xtensa port.

Provenance for the filled C6 numbers (all cross-checked on the bench, see #4):
  - ROM addrs: esp-idf hal_espressif components/esp_rom/esp32c6/ld/esp32c6.rom.ld
  - reset regs: openocd-esp32 tcl/target/esp32c6.cfg esp32c6_soc_reset (LP_AON)
  - SRAM map:  esp-idf components/soc/esp32c6/include/soc/soc.h (SOC_IRAM 0x40800000..0x40880000)
"""

# --- chain-layout presets (referenced by entries) ------------------------------
_SINGLE_TAP = dict(taps_after=0, taps_before=0, idcode_index=0)
# C5/C61: TDI -> tap1 (HP RISC-V, our target) -> tap0 (LP) -> TDO. Our target is
# tap1, so one BYPASS TAP (tap0) sits between it and TDO. Verified vs
# esp32c5-builtin.cfg (_HP_TAPNUM=1,_LP_TAPNUM=1) + a live -d3 chain interrogation,
# and re-confirmed on the bench: read_idcode returns the index-1 IDCODE 0x17c25.
_C5_TWO_TAP = dict(taps_after=1, taps_before=0, idcode_index=1)


CHIPS = {
    # ---- ESP32-C6 (single-TAP RISC-V) ----------------------------------------
    # VERIFIED on the bench (c6-maker 40:4C:CA:51:68:88, xiao-c6 E4:B0:63:41:C1:44):
    # IDCODE 0x0000dc25, DTMCS 0x00001071 (version 1, abits 7), dmstatus version 2,
    # single TAP. selftest 3/3. ROM addrs cross-checked vs esp32c6.rom.ld below.
    0x0000DC25: dict(
        name="C6",
        core="riscv",
        abits=7,
        chain=_SINGLE_TAP,
        flash_xip=0x42000000,
        # SoC reset regs used by reset_run_from_rom (LP_AON, TRM "System Reset").
        # 0x600b1034 bit31 = LP_AON_HPSYS_SW_RESET; 0x600b1038 bit28 = CPU_CORE0.
        # Source: openocd-esp32 tcl/target/esp32c6.cfg esp32c6_soc_reset.
        reset=dict(
            hpsys_cfg=0x600B1034, hpsys_sw_reset=0x80000000,
            cpucore0_cfg=0x600B1038, cpucore0_sw_reset=0x10000000,
        ),
        # ROM SPI-flash helpers + cache control (esp-idf esp32c6.rom.ld) for the
        # flash loader (#3). VERIFIED against the .ld at
        #   components/esp_rom/esp32c6/ld/esp32c6.rom.ld
        # esp_rom_spiflash_write(a0=dest_byte_addr, a1=src_sram_ptr, a2=len) does
        # WREN / WIP-poll / 256-byte paging itself; addr+len must be 4-byte aligned.
        # esp_rom_spiflash_erase_sector(a0=sector_index = byte_addr/0x1000).
        # esp_rom_spiflash_read(a0=src_byte_addr, a1=dest_sram_ptr, a2=len) — used
        # to read flash back for verification WITHOUT touching the XIP cache window.
        # Result in a0: 0=OK, 1=ERR, 2=TIMEOUT (esp_rom_spiflash_result_t).
        rom=dict(
            spiflash_unlock=0x40000154,
            spiflash_erase_sector=0x40000144,
            spiflash_write=0x4000014C,
            spiflash_read=0x40000150,
            spiflash_wait_idle=0x40000110,
            spiflash_read_status=0x40000180,
            cache_disable_icache=0x40000690,
            cache_enable_icache=0x40000694,
        ),
        # A scratch SRAM window the flash loader uses while the hart is HALTED.
        # C6 SRAM is a unified byte-addressable 512 KiB block 0x40800000..0x40880000
        # (soc.h SOC_IRAM_LOW/HIGH). We carve the TOP for scratch — the app image
        # links from the LOW end, so the high pages are the least likely to be
        # live. `data` = the staging buffer + a word holding ebreak (0x00100073)
        # for the return trap (`trap`); `stack` = a downward-growing SP. Kept well
        # clear of each other. (Belt-and-braces: the loader reads+restores any
        # bytes it scribbles, since the app resumes into this RAM.)
        sram=dict(
            data=0x4087C000,     # staging buffer base (grows up; < trap)
            trap=0x4087BF00,     # ebreak return-trap word (below data, ra points here)
            stack=0x4087F000,    # SP top (grows down toward data; 12 KiB headroom)
            top=0x40880000,      # SOC_IRAM_HIGH (bound; never write at/above)
        ),
    ),

    # ---- ESP32-C5 / C61 (two-TAP RISC-V) -------------------------------------
    # VERIFIED on the bench (xiao-c5 38:44:BE:A5:29:98, read-only): IDCODE
    # 0x00017c25, DTMCS 0x000070a1 (version 1, abits 10), dmstatus version 2,
    # two-TAP chain (target on tap1). selftest 3/3 read-only.
    0x00017C25: dict(
        name="C5",
        core="riscv",
        abits=10,
        chain=_C5_TWO_TAP,
        flash_xip=0x42000000,
        # rom/reset/sram TODO from esp32c5.rom.ld / esp32c5.cfg / esp32c5 soc.h.
        # Left UNSET deliberately: reset_run_from_rom and the flash loader on C5
        # are NOT yet verified, and a wrong ROM/reset address would wedge the part.
        # (esp32c5.rom.ld exists at components/esp_rom/esp32c5/ld/ — fill + bench-
        #  verify before enabling flash on C5.)
    ),

    # ---- ESP32-C3 (single-TAP RISC-V) ----------------------------------------
    # UNVERIFIED: no C3 was on the bench when this was tabled (xiao-c3 unplugged).
    # The IDCODE below is a GUESS extrapolated from the C6/C5 pattern (..C25) and
    # MUST be confirmed by reading a real C3 before relying on it. abits is assumed
    # 7 (single-TAP like the C6) but UNCONFIRMED. rom/reset/sram intentionally
    # empty — do NOT guess ROM/reset addresses (a wrong one wedges the chip);
    # fill from esp32c3.rom.ld + esp32c3.cfg + soc.h once a C3 is read.
    0x0000_5C25: dict(            # TODO(#4) confirm the C3 IDCODE on the bench — GUESS
        name="C3",
        core="riscv",
        abits=7,                  # ASSUMED (single-TAP), UNVERIFIED
        chain=_SINGLE_TAP,
        flash_xip=0x42000000,
        verified=False,
    ),

    # ---- Xtensa parts (#9 — same USB transport, DIFFERENT debug module) -------
    # These are recognised (core="xtensa") so tools can say "not a RISC-V part,
    # use the Xtensa path" instead of mis-reading them as RISC-V. The Xtensa OCD
    # debug module (NAR/NDR, instruction injection) is the #9 port; abits/rom/reset
    # are RISC-V concepts and don't apply. IDCODEs TODO-confirm on the bench.
    # 0x120034e5: dict(name="S3", core="xtensa", chain=_SINGLE_TAP),  # TODO verify
    # 0x120034e5: dict(name="S2", core="xtensa", chain=_SINGLE_TAP),  # TODO verify
}


def lookup(idcode):
    """Return the CHIPS entry for a target-TAP IDCODE, or None if unknown.
    The C5 two-TAP chain is keyed by its target (tap1) IDCODE 0x17c25."""
    return CHIPS.get(idcode & 0xFFFFFFFF)


def name_for(idcode):
    """Short chip label for an IDCODE, or None if unknown."""
    e = lookup(idcode)
    return e["name"] if e else None


def abits_for(idcode):
    """Expected DTMCS abits for an IDCODE, or None if unknown/not a RISC-V part."""
    e = lookup(idcode)
    return e.get("abits") if e else None


def chain_for(idcode):
    """Chain-layout dict for an IDCODE (defaults to single-TAP if unknown so a new
    single-TAP part still works before it's tabled)."""
    e = lookup(idcode)
    return (e.get("chain") if e else None) or dict(_SINGLE_TAP)


def chain_by_idcode():
    """{idcode: chain-layout} for every NON-single-TAP entry — what the transport's
    chain auto-detect keys on (a single-TAP part is the default and needn't be
    listed). Lets transport.py stay data-free."""
    out = {}
    for idc, e in CHIPS.items():
        ch = e.get("chain") or _SINGLE_TAP
        if ch != _SINGLE_TAP:
            out[idc] = dict(ch)
    return out


def rom_for(idcode):
    """The ROM-symbol dict for the flash loader (#3), or None if not tabled.
    Absent = flash-over-JTAG is not yet wired/verified for this part."""
    e = lookup(idcode)
    return e.get("rom") if e else None


def reset_for(idcode):
    """The SoC reset-register dict (LP_AON SW-reset), or None if not tabled."""
    e = lookup(idcode)
    return e.get("reset") if e else None


def sram_for(idcode):
    """The scratch-SRAM window dict for the flash loader, or None if not tabled."""
    e = lookup(idcode)
    return e.get("sram") if e else None


def is_riscv(idcode):
    e = lookup(idcode)
    return bool(e) and e.get("core") == "riscv"


def is_verified(idcode):
    """False for entries explicitly flagged verified=False (e.g. the C3 IDCODE
    guess); True for a tabled, bench-confirmed part; False for unknown IDCODEs."""
    e = lookup(idcode)
    return bool(e) and e.get("verified", True)
