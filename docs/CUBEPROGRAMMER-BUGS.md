# STM32CubeProgrammer 2.22.0 — two `incremental` bugs

Found on Linux x86-64, v2.22.0, STM32F427 + ST-LINK V3. Both in the `-d … incremental`
("new write mechanism") path.

---

## Bug 1 — `incremental` silently drops sum-preserving changes (data loss)

Based on the shipped flash-loader stubs (`bin/{FlashLoader,ExternalLoader}/*.stldr`,
SLA0048, ARM-Thumb), the per-sector `CheckSum` **appears to be a 32-bit additive
byte-sum** (`adds` over the bytes, zero CRC/`eor` ops — internal and MX25 QSPI loaders
alike). So any edit that preserves a sector's byte-sum — a `+1/−1` pair, a byte swap, a
reorder, all routine compiler output — is judged "unchanged" and **never written**.

**Proof** — real STM32F427, black-box (`scripts/st_incremental_proof.py`):

```
flash A;  then  `incremental` flash B.
In one 16 KB sector B swaps two bytes  (0x11,0x22 -> 0x22,0x11):
content differs, additive sum identical.

  incremental :  read 0x08004100 -> 0x11   B's 0x22 SILENTLY DROPPED
  normal mode :  read 0x08004100 -> 0x22   same B, written
```

CubeProgrammer's own log erases only the sum-*changed* control sector:
`Identify the modified sectors … Erasing internal memory sector 2` — the swapped
sector is never erased. Reproducible; the normal-mode column is the control (same image).

---

## Bug 2 — `incremental` write path use-after-free with a 2nd probe (crash)

With **two ST-LINK probes** attached, an `incremental` download on one **SIGSEGVs** when
a USB hotplug/enumeration event from the *other* fires mid-write — the global loader
table is rebuilt+freed under the active flash write (a use-after-free), crashing in the
flash programmer (`ST_LINKInterface::programMemory`), deterministic across 21 coredumps.
Single process; the 2nd probe is never commanded — its USB presence is enough.

**Repro:**

```
STM32_Programmer_CLI -c port=SWD sn=<probeA> mode=UR -d fw.elf <addr> incremental
#  while it runs, plug in (or USB-power-thrash) a 2nd ST-LINK  ->  SIGSEGV
```

Workaround: omit `incremental` (full-flash mode skips this path entirely).
RE detail + coredumps: `~/git/gihdra` (GH #30).

---

*Bug 1 evidence: the shipped flash-algorithm stubs + a black-box differential flash
whose setup (write A) and **every read-back** go through the open-source `st-flash`
(texane, BSD) — **not** CubeProgrammer. So the result is read by an independent tool,
never self-certified by the tool under test, and no host binary is decompiled.*
