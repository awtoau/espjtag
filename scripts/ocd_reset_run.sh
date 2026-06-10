#!/usr/bin/env bash
# Run OpenOCD `reset run` against xiao-c6-b (USB loc 1-1.3.1.3.4) with an optional
# debug level, capturing the full trace. This is BOTH the recovery path (OpenOCD
# reliably boots the C6 out of post-flash ROM) AND the ground-truth DMI capture
# for porting the sequence into espjtag.
#
# Usage: ocd_reset_run.sh [debug_level]   (e.g. 3 for -d3; default: no -d flag)
set -euo pipefail
OCD=/home/dan/.espressif/tools/openocd-esp32/v0.12.0-esp32-20251215/openocd-esp32
DLEVEL="${1:-}"
DFLAG=()
if [ -n "$DLEVEL" ]; then DFLAG=(-d"$DLEVEL"); fi
exec "$OCD/bin/openocd" -s "$OCD/share/openocd/scripts" "${DFLAG[@]}" \
    -c "adapter usb location 1-1.3.1.3.4" \
    -f board/esp32c6-builtin.cfg \
    -c "init; reset run; exit"
