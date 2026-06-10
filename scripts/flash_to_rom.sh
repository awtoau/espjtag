#!/usr/bin/env bash
# Flash xiao-c6-b into ROM download mode (app NOT booted), the way the task asks.
# --after no-reset declines to reboot, so the only thing that boots the app is OUR
# reset code under test.  --before default-reset enters the ROM downloader to write.
#
# A second mode (`hardreset`) mirrors what dev.py usb-flash actually does
# (--before usb-reset --after hard-reset -z): on the C6 the hard-reset is a
# documented USJ no-op for strapping, so the chip STILL ends up stranded in ROM,
# but the path differs (no resident download stub the way --after no-reset leaves).
# Used to A/B which post-flash ROM state OpenOCD/espjtag can actually boot out of.
set -euo pipefail
TTY="$(readlink -f /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_58:E6:C5:11:B7:EC-if00)"
BIN=/home/dan/git/esp32-zephyr/builds/xiao-c6-b/zephyr/zephyr.bin
MODE="${1:-noreset}"
if [ "$MODE" = "hardreset" ]; then
    echo "flashing $BIN to $TTY (usb-flash style: usb-reset/hard-reset/-z)..."
    esptool --chip esp32c6 --port "$TTY" --before usb-reset --after hard-reset \
        --connect-attempts 1 write-flash -z 0x0 "$BIN"
else
    echo "flashing $BIN to $TTY (leave in ROM: default-reset/no-reset)..."
    esptool --chip esp32c6 --port "$TTY" --before default-reset --after no-reset \
        write-flash 0x0 "$BIN"
fi
