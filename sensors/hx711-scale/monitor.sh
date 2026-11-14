#!/bin/sh
# Show the scale's log over USB, without resetting it.
#   sensors/hx711-scale/monitor.sh                 # finds the port itself
#   sensors/hx711-scale/monitor.sh /dev/cu.usbmodem142401
# Leave with Ctrl-]. Type a command (j, J, i 300, ...) and press Enter to send it.
set -e
cd "$(dirname "$0")"
port="$1"
if [ -z "$port" ]; then
    # The C6's own USB port; the Zigbee dongle shows up as usbserial instead.
    set -- /dev/cu.usbmodem*
    [ -e "$1" ] || { echo "No /dev/cu.usbmodem* port: is the scale plugged into this Mac with a data cable?" >&2; exit 1; }
    [ $# -eq 1 ] || { echo "Several ports, pick one: $*" >&2; exit 1; }
    port="$1"
fi
. "${IDF_PATH:-$HOME/esp/esp-idf}/export.sh" >/dev/null 2>&1
exec idf.py -p "$port" monitor --no-reset
