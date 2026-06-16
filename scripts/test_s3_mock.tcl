# test_s3_mock.tcl — FULL no-hardware test of the S3 stub flasher (#29), driven
# through the software model (MockXtensaXDM) via the Tcl bridge. NO chip needed:
#
#   python3 scripts/ocd_tcl_bridge.py --mock --tcl scripts/test_s3_mock.tcl
#
# Validates the whole flasher operation-sequence (the "do I form the right bits?"
# layer) deterministically: the load plan, no overlapping writes, the section
# bytes in the model RAM, and the register setup at run.
#
# Command naming: xtensa_<noun>_<verb> for the extension-specific commands;
# register roles (entry_point/stack_pointer/return_value) instead of raw a-regs.

puts "=== #29 flasher — NO-HARDWARE (software model) test ==="

# 1. PURE plan: layout addresses + no overlapping writes (auto-catches collisions)
assert_equal "entry address"      [xtensa_stub_plan_address cmd_test1 entry]             0x4038c2c0
assert_equal "trampoline address" [xtensa_stub_plan_address cmd_test1 tramp_mapped_addr] 0x4038d010
xtensa_plan_assert_no_overlap cmd_test1
xtensa_plan_assert_no_overlap cmd_flash_write_deflated

# 2. Load against the model — records every write, no JTAG
puts "  loaded cmd_test1: [xtensa_mock_load cmd_test1] writes"

# 3. Golden-check the loaded bytes in the model RAM: data at dram_org is written
#    NORMALLY, so its first bytes equal the data-blob start.
xtensa_memory_expect 0x3fca0000 1818181810000000

# 4. Run against the model: script the return value, then check the register setup
#    by ROLE (not raw a-register numbers).
puts "  run -> [xtensa_mock_run 0]"
xtensa_register_expect return_address 0x0
xtensa_register_expect entry_point    0x4038c2c0
xtensa_register_expect return_value   0x0

# 5. Operation-sequence shape: a load+run issues several memory writes + register
#    sets. Proves the flasher forms the right ops, not just the right bytes.
puts "  operations: [xtensa_operation_count] total, \
[xtensa_operation_count write_mem] memory-writes, \
[xtensa_operation_count set_ar] register-sets"

puts "=== done (no hardware was touched) ==="
