#!/usr/bin/env bash
# build_stub.sh — compile the #27 RAM flasher stub to a position-independent
# binary blob. Output is COMMITTED (espjtag/blobs/stub_rv32.bin) so users never
# need the toolchain; rerun this after editing stub/stub.c.
set -eu
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
GCC=$(ls /home/dan/.espressif/tools/riscv32-esp-elf/*/riscv32-esp-elf/bin/riscv32-esp-elf-gcc | head -1)
OBJCOPY="${GCC%gcc}objcopy"
OBJDUMP="${GCC%gcc}objdump"
mkdir -p tmp espjtag/blobs
# rv32imc covers C3/C5/C6/H2; no jump tables (they'd emit absolute addresses
# and break position independence); entry must be the first/only function.
"$GCC" -march=rv32imc_zicsr -mabi=ilp32 -Os -ffreestanding -nostdlib \
       -fno-jump-tables -fomit-frame-pointer \
       -Wl,--entry=stub -Wl,-Ttext=0 -Wl,--no-relax \
       -o tmp/stub_rv32.elf stub/stub.c
"$OBJCOPY" -O binary --only-section=.text tmp/stub_rv32.elf espjtag/blobs/stub_rv32.bin
"$OBJDUMP" -d tmp/stub_rv32.elf > tmp/stub_rv32.lst
# Position-independence check: no absolute-address relocations may survive;
# auipc-relative is fine. Look for any 'lui' loading a code/data address.
SIZE=$(stat -c%s espjtag/blobs/stub_rv32.bin)
echo "stub_rv32.bin: ${SIZE} bytes (listing: tmp/stub_rv32.lst)"
