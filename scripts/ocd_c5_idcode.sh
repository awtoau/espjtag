#!/usr/bin/env bash
# Ground-truth IDCODE/DTMCS for the C5 via OpenOCD. Read-only init then exit.
# Usage: ocd_c5_idcode.sh <usb-location>  e.g. 1-1.2
set -euo pipefail
LOC="${1:?usb location required, e.g. 1-1.2}"
OCD_ROOT=/home/dan/.espressif/tools/openocd-esp32/v0.12.0-esp32-20251215/openocd-esp32
"$OCD_ROOT/bin/openocd" \
  -s "$OCD_ROOT/share/openocd/scripts" \
  -c "adapter usb location $LOC" \
  -f board/esp32c5-builtin.cfg \
  -c "init" \
  -c "exit"
