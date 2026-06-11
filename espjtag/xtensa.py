"""espjtag.xtensa — ESP32-S3 Xtensa XDM debug access over the SAME esp_usb_jtag
transport as the RISC-V path (issue #9).

espjtag is RISC-V-native (RISC-V Debug Module). The Xtensa parts (S2/S3) use a
DIFFERENT debug module — the Xtensa On-Chip-Debug (XDM), reached via the Nexus
address/data register (NAR) — so this adds that protocol: NAR register read/write,
halt, and memory read/write via DIR0EXEC instruction injection. The S3 is then
reachable on the same transport (and tablable as a Tcl-bridge leaf command).

Everything is transcribed verbatim from openocd-esp32 src/target/xtensa/xtensa.c +
xtensa_debug_module.c, and validated on the bench register-for-register against
OpenOCD's own `drscan`/`irscan` and a live probe-rs memory read.

Two things make it work over espjtag's bit-banged transport:
  * the S3 is a TWO-TAP chain (chips.py tables it; the transport inserts the other
    TAP's BYPASS bit on every IR/DR scan);
  * a NAR access is ONE IR=NARSEL select then a single 8-bit addr scan ((nar<<1)|rw)
    + 32-bit data scan — and espjtag's transport lags the NAR read result by
    READ_LATENCY accesses vs OpenOCD's driver, flushed with priming reads.
"""

# TAP instructions + NAR register addresses (xtensa_debug_module.c/.h)
IR_PWRCTL, IR_PWRSTAT, IR_NARSEL = 0x08, 0x09, 0x1C
DCRSET, DCRCLR, DSR, DDR, DDREXEC, DIR0EXEC = 0x43, 0x42, 0x44, 0x45, 0x46, 0x47
OCDID = 0x40
OCDDCR_ENABLEOCD, OCDDCR_DEBUGINTERRUPT, OCDDSR_STOPPED = 0x01, 0x02, 0x10
# Xtensa instruction encodings, little-endian (DDR special reg 0x68, a3 = reg 3)
INS_RSR_DDR_A3 = 0x036830     # RSR  a3, DDR   (DDR special reg -> a3)
INS_WSR_DDR_A3 = 0x136830     # WSR  a3, DDR   (a3 -> DDR)
INS_LDDR32P_A3 = 0x0073E0     # LDDR32.P a3    (mem[a3] -> DDR, a3 += 4)
INS_SDDR32P_A3 = 0x0073F0     # SDDR32.P a3    (DDR -> mem[a3], a3 += 4)
IDLE = 16                     # run-test-idle settle clocks (the XDM tdi_idle)
# READ_LATENCY = espjtag's NAR read-result pipeline depth over this transport: the
# captured data for a NAR read lags its address scan by this many *accesses*. We
# issue address+data as two separate DR scans (split _send()s) and capture TDO a
# scan late, while OpenOCD's driver pipelines the pair, so over the bit-banged
# transport the word that comes back belongs to the access TWO earlier. Hence every
# read does READ_LATENCY+1 priming reads (nar_read) or over-reads nwords+READ_LATENCY
# and slices off the leading READ_LATENCY (read_mem). 2 is empirical — it is the
# value that makes the golden ROM read @0x40000000 land correctly on the bench; it
# is NOT a guess but is transport-specific, so leave it unless re-validated on HW.
READ_LATENCY = 2


class XtensaXDM:
    """Xtensa OCD access on an EspUsbJtag transport `j`. The S3 must be tabled
    two-TAP in chips.py so the transport carries the BYPASS bit per scan."""

    def __init__(self, j):
        self.j = j
        j.read_idcode()                       # trigger the two-TAP chain detect

    # --- NAR register access: ONE IR=NARSEL, single 8-bit addr + 32-bit data -----
    def nar_read(self, naraddr):
        """Read a side-effect-free XDM register (OCDID/DSR/DDR — NOT DDREXEC, which
        re-triggers DIR0 on each read). Reads READ_LATENCY+1 times to flush espjtag's
        NAR read latency and returns the last."""
        j = self.j
        j._drain_in()
        j._scan_ir(IR_NARSEL)
        val = 0
        for _ in range(READ_LATENCY + 1):
            j._scan_dr((naraddr << 1) | 0, 8)
            t = j._scan_dr(0, 32, capture=True)
            j._send()
            val = j._dr_field(j._recv(t), 0, 32)
        return val

    def nar_write(self, naraddr, value):
        j = self.j
        j._drain_in()
        j._scan_ir(IR_NARSEL)
        j._scan_dr((naraddr << 1) | 1, 8)
        j._scan_dr(value & 0xFFFFFFFF, 32)
        j._idle(IDLE)
        j._send()

    # --- power up the debug domain / halt / resume -------------------------------
    def powerup(self):
        """xtensa_examine: PWRCTL=0x07, 0x87 (| JTAGDEBUGUSE), then DCRSET=ENABLEOCD,
        as ONE batch (a TLR or split would lose the power state)."""
        j = self.j
        j._drain_in()
        j._scan_ir(IR_PWRCTL)
        j._scan_dr(0x07, 8); j._idle(IDLE)
        j._scan_dr(0x87, 8); j._idle(IDLE)
        j._scan_ir(IR_NARSEL)
        j._scan_dr((DCRSET << 1) | 1, 8)
        j._scan_dr(OCDDCR_ENABLEOCD, 32); j._idle(IDLE)
        j._send()

    def halt(self, tries=200):
        self.nar_write(DCRSET, OCDDCR_ENABLEOCD | OCDDCR_DEBUGINTERRUPT)
        for _ in range(tries):
            if self.nar_read(DSR) & OCDDSR_STOPPED:
                return True
        return False

    def resume(self):
        self.nar_write(DCRCLR, OCDDCR_DEBUGINTERRUPT)

    # --- a3 save/restore around instruction injection (core must be HALTED) -------
    # read_mem/write_mem stage the target address in a3 and stream LDDR32.P/SDDR32.P
    # off it, which CLOBBERS a3. If the app was merely halted (not reset) we must
    # hand a3 back unchanged on resume, so each access brackets itself with these.
    # Both are built only from the already-validated nar_read/nar_write primitives
    # (each does its own IR=NARSEL select), so they are correct by reuse and need no
    # new latency bookkeeping — nar_read primes the READ_LATENCY pipeline itself.
    def _save_a3(self):
        """Return the live a3. WSR a3,DDR copies a3 into DDR (the inverse of the
        RSR DDR,a3 the streaming uses); then read DDR back, latency-primed."""
        self.nar_write(DIR0EXEC, INS_WSR_DDR_A3)   # DDR = a3   (exec WSR a3, DDR)
        return self.nar_read(DDR)                   # a3 value, READ_LATENCY-primed

    def _restore_a3(self, saved):
        """Put `saved` back into a3. DDR = saved, then RSR DDR,a3 moves it to a3 —
        the same two-step staging read_mem/write_mem use for the address."""
        self.nar_write(DDR, saved)                  # DDR = saved a3
        self.nar_write(DIR0EXEC, INS_RSR_DDR_A3)    # a3 = DDR = saved (exec RSR)

    # --- memory via DIR0EXEC instruction injection (core must be HALTED) ----------
    def read_mem(self, addr, nwords):
        """Read nwords 32-bit words from `addr`. Stage addr in a3 (RSR DDR,a3), exec
        LDDR32.P a3 once (DDR=mem[a3], a3+=4), then stream: read DDREXEC (returns the
        word AND re-triggers the load) for all but the last, plain DDR for the last.
        Saves+restores a3 so a halted app resumes intact. (One IR=NARSEL; single
        addr+data per access; latency primed.)"""
        j = self.j
        saved_a3 = self._save_a3()                # preserve the app's a3
        j._drain_in()
        j._scan_ir(IR_NARSEL); j._send()

        def acc(naraddr, rw, data=0, cap=False):
            j._scan_dr((naraddr << 1) | rw, 8); j._send()
            t = j._scan_dr(data & 0xFFFFFFFF, 32, capture=cap); j._send()
            return j._dr_field(j._recv(t), 0, 32) if cap else None

        acc(DDR, 1, addr)                         # DDR = addr
        acc(DIR0EXEC, 1, INS_RSR_DDR_A3)          # a3 = DDR = addr
        acc(DIR0EXEC, 1, INS_LDDR32P_A3)          # DDR = mem[a3]; a3 += 4 (word0)
        total = nwords + READ_LATENCY
        out = [acc(DDR if k == total - 1 else DDREXEC, 0, 0, cap=True)
               for k in range(total)]
        self._restore_a3(saved_a3)                # hand a3 back to the app
        return out[READ_LATENCY:READ_LATENCY + nwords]

    def write_mem(self, addr, words):
        """Write `words` from `addr`. Stage addr in a3, then per word: DDR=word, exec
        SDDR32.P a3 (mem[a3]=DDR, a3+=4). Saves+restores a3 so a halted app resumes
        intact."""
        j = self.j
        words = list(words)
        saved_a3 = self._save_a3()                # preserve the app's a3
        j._drain_in()
        j._scan_ir(IR_NARSEL); j._send()

        def acc(naraddr, rw, data=0):
            j._scan_dr((naraddr << 1) | rw, 8); j._send()
            j._scan_dr(data & 0xFFFFFFFF, 32); j._send()

        acc(DDR, 1, addr)                         # DDR = addr
        acc(DIR0EXEC, 1, INS_RSR_DDR_A3)          # a3 = DDR = addr
        for w in words:
            acc(DDR, 1, w)                        # DDR = word
            acc(DIR0EXEC, 1, INS_SDDR32P_A3)      # mem[a3] = DDR; a3 += 4
        self._restore_a3(saved_a3)                # hand a3 back to the app
        return len(words)

    def read_mem32(self, addr):
        return self.read_mem(addr, 1)[0]

    def write_mem32(self, addr, value):
        self.write_mem(addr, [value])
