# test_s3_mock.tcl — FULL no-hardware test of the S3 stub flasher (#29), driven
# through the MockXtensaXDM via the Tcl bridge. Runs with NO chip on the bus:
#
#   python3 scripts/ocd_tcl_bridge.py --mock --tcl scripts/test_s3_mock.tcl
#
# Validates the whole flasher op-sequence (the "do I form the right bits?" layer)
# deterministically: the load plan, no overlaps, the reversed/normal section
# bytes in the model RAM, and the start/wait_algorithm register dance.

puts "=== #29 flasher — NO-HARDWARE (mock) test ==="

# 1. PURE plan: layout addresses + no overlapping writes (auto-catches collisions)
proc check {label got want} { assert_eq $label $got $want }
check "entry addr"  [stub_plan cmd_test1 entry]             0x4038c2c0
check "tramp addr"  [stub_plan cmd_test1 tramp_mapped_addr] 0x4038d010
plan_no_overlap cmd_test1
plan_no_overlap cmd_flash_write_deflated

# 2. Load against the mock — records every write, no JTAG
puts "  loaded cmd_test1: [mock_load cmd_test1] writes"

# 3. Golden-check the loaded bytes in the model RAM:
#    - data at dram_org is written NORMALLY -> first bytes = the data blob start
mem_expect 0x3fca0000 1818181810000000
#    - code is reversed; the reversed first chunk lands at the top of the IRAM
#      area. (Spot-check the data normal-write; reversed code is covered by the
#      reverse_binary pure vectors in test_s3_stub.tcl.)

# 4. Run against the mock: script a2=0, check the register dance
puts "  run -> [mock_run 0]"
reg_expect a0 0x0
reg_expect a8 0x4038c2c0
reg_expect a2 0x0
#    op shape: a load+run should issue several write_mem + the reg sets + 1 resume
puts "  ops: [op_count] total, [op_count write_mem] writes, [op_count set_ar] reg-sets"

puts "=== done (no hardware was touched) ==="
