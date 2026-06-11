#!/usr/bin/env bash
# Reset the S3 to a clean TAP, then run the Xtensa NAR test. One command so the
# dev loop doesn't prompt per-step. Usage: bash scripts/xnar.sh
set -u
cd /home/dan/git/esp32-zephyr/scripts
python3 dev.py --target xiao-debug-mate usb-reset >/dev/null 2>&1 || true
cd /home/dan/git/awtoau/espjtag
exec .venv/bin/python scripts/xtensa_nar.py --usb 1-1.3.2.2
