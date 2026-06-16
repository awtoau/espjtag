# test_s3_stub.tcl — step-by-step S3 stub-flasher test (#29), run through the
# espjtag OcdTclBridge (our pure-Python Tcl + OpenOCD leaf commands on the Xtensa
# XDM). The "do it the OpenOCD way" test: Tcl drives stub_load/stub_run.
#
#   python3 scripts/ocd_tcl_bridge.py --usb 1-1.3.3.4 --tcl scripts/test_s3_stub.tcl
#
# Each `step` prints PASS/FAIL; `expect` compares got vs want.

proc expect {label got want} {
    if {[string equal $got $want]} {
        puts "  \[PASS\] $label -> $got"
    } else {
        puts "  \[FAIL\] $label -> got '$got' want '$want'"
    }
}

proc report {label got} {
    puts "  \[INFO\] $label -> $got"
}

proc check {label got want} {
    expect $label [compare $got $want] 1
}

puts "=== PURE layout validation (NO JTAG — plan_load only) ==="
# These assert the load LAYOUT deterministically, with zero hardware side-effects
# — the part that paraphrase-bugs hid. Golden values are from the 1:1 port.
check "cmd_test1 entry addr"   [stub_plan cmd_test1 entry]             0x4038c2c0
check "cmd_test1 tramp addr"   [stub_plan cmd_test1 tramp_mapped_addr] 0x4038d010
check "cmd_test1 stack addr"   [stub_plan cmd_test1 stack_addr]        0x3fca0530
check "cmd_test1 dram_org"     [stub_plan cmd_test1 dram_org]          0x3fca0000
# data is written NORMAL at dram_org (first 8 bytes = the data blob start)
check "data@dram_org (normal)" [stub_plan cmd_test1 wbytes 6 8]        1818181810000000
report "cmd_test1 nwrites" [stub_plan cmd_test1 nwrites]

puts "=== ON-TARGET steps (JTAG) ==="

# 1. halt the core
set h [xhalt]
expect "xhalt (core halted)" $h 1

# 2. memory round-trip through the XDM via Tcl mww/mdw (proves the transport the
#    stub loader uses). Write 4 words to spare DRAM, read back.
mww 0x3fca8000 0xdeadbeef
mww 0x3fca8004 0x01234567
report "mdw 0x3fca8000 2" [mdw 0x3fca8000 2]

# 3. load cmd_test1 (the simplest stub) — returns 'entry stack tramp'
report "stub_load cmd_test1" [stub_load cmd_test1]

# 4. run cmd_test1: must halt at the exit BREAK and return a2=0 (success). This
#    is the #29 core: the windowed stub runs natively + re-halts.
set rc [stub_run]
expect "stub_run cmd_test1 (a2==0, halted)" $rc 0x0

# 5. dispatch: load + run cmd_flash_map_get with cmd=7 + appimage_base=0. A real
#    handler runs (returns a defined code, not TIMEOUT) — proves args + dispatch.
report "stub_load cmd_flash_map_get" [stub_load cmd_flash_map_get]
set mrc [stub_run 7 0]
if {[string equal $mrc TIMEOUT]} {
    puts "  \[FAIL\] dispatch flash_map_get -> TIMEOUT (stub hung)"
} else {
    puts "  \[PASS\] dispatch flash_map_get -> handler ran, a2=$mrc"
}

puts "=== done ==="
