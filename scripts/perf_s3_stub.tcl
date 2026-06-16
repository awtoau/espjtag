# perf_s3_stub.tcl — the SAME Tcl harness as a PERFORMANCE instrument (#29).
# Demonstrates: time_mark/time_elapsed_ms time a span, jtag_transaction_count
# counts bridge transactions, assert_less_than gates on a threshold. Performance
# becomes a Tcl-scriptable, asserted test — not just a one-off print.
#
#   python3 scripts/ocd_tcl_bridge.py --usb 1-1.3.3.4 --tcl scripts/perf_s3_stub.tcl

puts "=== PURE: plan cost (no JTAG) ==="
# Time how long computing the load plan takes (host-side only): the reversed-
# memory math + section chunking — should be ~instant.
time_mark plan
xtensa_stub_plan_writes cmd_flash_write_deflated
puts "  plan(cmd_flash_write_deflated) computed in [time_elapsed_ms plan] ms"

puts "=== ON-TARGET: load throughput ==="
if {[xtensa_core_halt] == 1} {
    # Time a real stub load (writes code+trampoline+data over JTAG).
    time_mark load
    xtensa_stub_load cmd_flash_write_deflated
    set load_ms [time_elapsed_ms load]
    puts "  xtensa_stub_load(cmd_flash_write_deflated, 6KB+) took $load_ms ms over JTAG"
    # A loose ceiling: a few KB over USB-JTAG should be well under 500 ms.
    assert_less_than "stub load under 500ms" $load_ms 500

    # Time a bridge mdw burst + report its transaction count.
    time_mark burst
    mdw 0x40000000 64
    puts "  mdw 64 words: [time_elapsed_ms burst] ms, \
[jtag_transaction_count burst] bridge transactions"
} else {
    puts "  (core did not halt — skipping on-target perf)"
}
puts "=== done ==="
