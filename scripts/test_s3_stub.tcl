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

puts "=== S3 stub-flasher steps (#29) ==="

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
