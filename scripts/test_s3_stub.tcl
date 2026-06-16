# test_s3_stub.tcl — step-by-step S3 stub-flasher test (#29), run through the
# espjtag OcdTclBridge (our pure-Python Tcl + OpenOCD leaf commands on the Xtensa
# Debug Module). The "do it the OpenOCD way" test: Tcl drives the flasher.
#
#   python3 scripts/ocd_tcl_bridge.py --usb 1-1.3.3.4 --tcl scripts/test_s3_stub.tcl
#
# Command naming: xtensa_<noun>_<verb> for the Xtensa-extension commands; generic
# assert_equal / report for the harness.

proc report {label got} { puts "  \[INFO\] $label -> $got" }

puts "=== PURE layout validation (NO JTAG — plan only) ==="
# These assert the load LAYOUT deterministically, with zero hardware side-effects
# — the part that transcription bugs hide in. Golden values are from the 1:1 port.
assert_equal "cmd_test1 entry address"   [xtensa_stub_plan_address cmd_test1 entry]             0x4038c2c0
assert_equal "cmd_test1 tramp address"   [xtensa_stub_plan_address cmd_test1 tramp_mapped_addr] 0x4038d010
assert_equal "cmd_test1 stack address"   [xtensa_stub_plan_address cmd_test1 stack_addr]        0x3fca0530
assert_equal "cmd_test1 dram origin"     [xtensa_stub_plan_address cmd_test1 dram_org]          0x3fca0000
# data is written NORMAL at dram_org (first 8 bytes = the data-blob start)
assert_equal "data@dram_org (normal)"    [xtensa_stub_plan_bytes cmd_test1 6 8]                 1818181810000000
report "cmd_test1 planned writes" [xtensa_stub_plan_writes cmd_test1]

puts "=== ON-TARGET steps (JTAG) ==="

# 1. halt the core
assert_equal "core halted" [xtensa_core_halt] 1

# 2. memory round-trip through the Xtensa Debug Module via Tcl mww/mdw (the
#    transport the stub loader uses). Write 2 words to spare DRAM, read back.
mww 0x3fca8000 0xdeadbeef
mww 0x3fca8004 0x01234567
report "mdw 0x3fca8000 2" [mdw 0x3fca8000 2]

# 3. load cmd_test1 (the simplest stub) — returns 'entry stack trampoline'
report "xtensa_stub_load cmd_test1" [xtensa_stub_load cmd_test1]

# 4. run cmd_test1: must halt at the exit BREAK and return 0 (success). This is
#    the #29 core: the windowed stub runs natively + re-halts.
assert_equal "stub_run cmd_test1 (return 0, halted)" [xtensa_stub_run] 0x0

# 5. dispatch: load + run cmd_flash_map_get with command=7 + appimage_base=0. A
#    real handler runs (returns a defined code, not TIMEOUT) — proves args +
#    dispatch.
report "xtensa_stub_load cmd_flash_map_get" [xtensa_stub_load cmd_flash_map_get]
set map_result [xtensa_stub_run 7 0]
if {[value_equal $map_result TIMEOUT]} {
    puts "  \[FAIL\] dispatch flash_map_get -> TIMEOUT (stub hung)"
} else {
    puts "  \[PASS\] dispatch flash_map_get -> handler ran, returned $map_result"
}

puts "=== done ==="
