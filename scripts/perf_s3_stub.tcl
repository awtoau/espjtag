# perf_s3_stub.tcl — the SAME Tcl harness as a PERFORMANCE instrument (#29).
# Demonstrates: mark/elapsed time spans, jtag_count counts bridge transactions,
# assert_lt gates on a threshold. Perf becomes a Tcl-scriptable, asserted test —
# not just a one-off print.
#
#   python3 scripts/ocd_tcl_bridge.py --usb 1-1.3.3.4 --tcl scripts/perf_s3_stub.tcl

puts "=== PURE: plan cost (no JTAG) ==="
# Time how long computing the load plan takes (host-side only). This is the
# reversed-memory math + section chunking — should be ~instant.
mark plan
stub_plan cmd_flash_write_deflated nwrites
puts "  plan_load(flash_write_deflated) computed in [elapsed plan] ms"

puts "=== ON-TARGET: load throughput ==="
set h [xhalt]
if {$h == 1} {
    # Time a real stub load (writes code+tramp+data over JTAG) and count the
    # bridge transactions around it for context.
    mark load
    stub_load cmd_flash_write_deflated
    set ms [elapsed load]
    puts "  stub_load(flash_write_deflated, 6KB+) took $ms ms over JTAG"
    # A loose ceiling: a few KB over USB-JTAG should be well under 500 ms.
    assert_lt "stub_load under 500ms" $ms 500

    # Time a bridge mdw burst + report its transaction count.
    mark burst
    mdw 0x40000000 64
    puts "  mdw 64 words: [elapsed burst] ms, [jtag_count burst] bridge txns"
} else {
    puts "  (core did not halt — skipping on-target perf)"
}
puts "=== done ==="
