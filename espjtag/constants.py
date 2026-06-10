"""espjtag.constants — esp_usb_jtag protocol + RISC-V Debug Module register/field
definitions. Shared by the transport, reset, and debug layers.

Ported (hand-transcribed numeric facts, not copied code) from:
  - openocd-esp32  src/jtag/drivers/esp_usb_jtag.c   (GPL-2.0-or-later)
        the CMD_* nibble layout, VEND_JTAG_SETDIV, caps descriptor wValue
  - openocd-esp32  src/target/riscv/debug_defines.h  (BSD-2-Clause OR CC-BY-4.0)
        the RISC-V Debug Module register addresses + field bit-offsets
Pinned upstream commit + the exact symbols we depend on: ../upstream.lock.
Provenance + licensing analysis: ../ACKNOWLEDGEMENTS.md. Drift check:
scripts/check_upstream.py. Not affiliated with Espressif / OpenOCD / RISC-V.
"""

VID, PID = 0x303A, 0x1001
VENDOR_CLASS = 0xFF                        # the JTAG interface is vendor-spec;
                                           # iface 0/1 are the CDC console — NOT it
CAPS_DESCRIPTOR = 0x2000                   # GET_DESCRIPTOR wValue

# 4-bit OUT command nibbles (esp_usb_jtag.c)
def _clk(tms, tdi, cap=0):
    return (cap << 2) | (tms << 1) | tdi   # 0b0 cap tms tdi
CMD_FLUSH = 0xA


def _bits_to_int(bits):
    return sum(b << i for i, b in enumerate(bits))

# JTAG IR opcodes (5-bit on these RISC-V cores). NOTE: this client is RISC-V only
# (C3/C5/C6/H2). The Xtensa parts (S2/S3) use the SAME esp_usb_jtag USB transport
# but a DIFFERENT debug module (Xtensa OCD/TRAX, not the RISC-V Debug Module) — no
# DMI/dmcontrol/ndmreset — so reset_run() here does not apply to them (and the S3
# boots fine via esptool anyway).
IR_DTMCS = 0x10
IR_DMI = 0x11
IR_LEN = 5
# DMI op field: 0=nop, 1=read, 2=write. dmi scan = (address<<34)|(data<<2)|op
DMI_NOP = 0
DMI_READ = 1
DMI_WRITE = 2
# RISC-V Debug Module register addresses (DMI address space) — debug_defines.h
DMCONTROL = 0x10
DMSTATUS = 0x11
HARTINFO = 0x12
ABSTRACTCS = 0x16     # abstract command status (busy, cmderr)
COMMAND = 0x17        # abstract command (access register etc.)
ABSTRACTAUTO = 0x18
DATA0 = 0x04          # abstract data 0..N
PROGBUF0 = 0x20       # program buffer 0..N
# System Bus Access registers (memory r/w without a running hart)
SBCS = 0x38
SBADDRESS0 = 0x39
SBDATA0 = 0x3C
# sbcs status fields (RISC-V debug spec): sberror[14:12] (0 = no error, W1C),
# sbbusy[21], sbbusyerror[22] (a read/write was attempted while busy — W1C).
SB_SBERROR = 0x7 << 12
SB_SBBUSY = 1 << 21
SB_SBBUSYERROR = 1 << 22
# dmcontrol bits
DM_DMACTIVE = 1 << 0
DM_NDMRESET = 1 << 1
DM_ACKHAVERESET = 1 << 28          # 0x10000000
DM_HARTRESET = 1 << 29             # 0x20000000
DM_RESUMEREQ = 1 << 30            # 0x40000000
DM_HALTREQ = 1 << 31             # 0x80000000
# dmstatus bits
DM_ANYRUNNING = 1 << 10
DM_ALLHALTED = 1 << 9
DM_ALLRUNNING = 1 << 11
DM_ANYRESUMEACK = 1 << 16
DM_ALLRESUMEACK = 1 << 17
DM_ANYHAVERESET = 1 << 18
DM_ALLHAVERESET = 1 << 19
DM_ANYUNAVAIL = 1 << 12
DM_ALLUNAVAIL = 1 << 13
# abstractcs
ABS_BUSY = 1 << 12
ABS_CMDERR = 0x7 << 8
# command: access register
CMD_ACCESS_REGISTER = 0 << 24
AC_TRANSFER = 1 << 17
AC_WRITE = 1 << 16
AC_AARSIZE32 = 2 << 20
AC_POSTEXEC = 1 << 18
# CSR numbers
CSR_DCSR = 0x7B0
CSR_DPC = 0x7B1
# GPR register index in abstract-command regno space (0x1000 + gpr)
REG_GPR_BASE = 0x1000

# NOTE: the ESP32-C6 LP_AON software-reset registers (used by esp32c6_soc_reset)
# moved to the per-chip table — espjtag.chips CHIPS[0x0000dc25]["reset"] is now
# the single source of truth (#4). 0x600b1034 bit31 = LP_AON_HPSYS_SW_RESET,
# 0x600b1038 bit28 = LP_AON_CPU_CORE0_SW_RESET; source openocd-esp32
# tcl/target/esp32c6.cfg esp32c6_soc_reset. debug.py reads them via chips.reset_for().

# RISC-V `ebreak` instruction word. Used as a return-trap: point a halted hart's
# return address (ra) at a scratch SRAM word holding this, so when a called
# function returns it re-enters debug mode (the "call a function on the target"
# mechanism, #3). Also what OpenOCD's progbuf-call recipe lands on.
EBREAK = 0x00100073

# dcsr value OpenOCD writes after a reset (set_dcsr_ebreak): ebreakm|ebreaku set
# so an EBREAK in any privilege mode enters debug mode rather than trapping. Read
# (0xc3 on the C6 at reset), OR in the ebreak bits, write back -> 0x90c3. This is
# part of OpenOCD's reset-run handshake the bare-write list omitted.
DCSR_EBREAKU = 1 << 12                   # 0x1000
DCSR_EBREAKM = 1 << 15                   # 0x8000
DCSR_EBREAK_BITS = DCSR_EBREAKM | DCSR_EBREAKU   # -> turns 0xc3 into 0x90c3
