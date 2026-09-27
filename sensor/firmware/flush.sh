#!/bin/sh
set -eu

firmware_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=idf-env.sh
. "$firmware_dir/idf-env.sh"

usage()
{
    cat <<EOF
Usage: $0 [--port DEVICE] [--no-monitor]

Build and flash the XIAO ESP32-C3, then open its serial monitor.

Options:
  --port DEVICE  Use a specific serial port instead of auto-detection.
  --no-monitor   Return after flashing instead of opening the monitor.
  -h, --help     Show this help.

Environment:
  OPP_PORT       Serial port, equivalent to --port.
  OPP_VERBOSE    Set to 1 to show full build and flash output instead of a
                 progress bar.
  IDF_PATH       ESP-IDF installation; defaults to ~/esp/esp-idf.

Exit the monitor with Ctrl-].
EOF
}

port=${OPP_PORT:-}
monitor=1
while [ "$#" -gt 0 ]; do
    case "$1" in
        --port)
            [ "$#" -ge 2 ] || { printf '%s\n' '--port requires a device.' >&2; exit 2; }
            port=$2
            shift 2
            ;;
        --no-monitor)
            monitor=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown option: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ -z "$port" ]; then
    ports=
    port_count=0
    for candidate in /dev/cu.usbmodem* /dev/cu.usbserial* /dev/ttyACM* /dev/ttyUSB*; do
        if [ -e "$candidate" ]; then
            ports="${ports}${ports:+
}$candidate"
            port=$candidate
            port_count=$((port_count + 1))
        fi
    done

    if [ "$port_count" -eq 0 ]; then
        cat >&2 <<EOF
No ESP32 serial port was found.

Use a data-capable USB cable. For XIAO ESP32-C3 recovery, disconnect USB,
hold BOOT, reconnect USB, then release BOOT. Rerun this script afterward.
EOF
        exit 1
    fi

    if [ "$port_count" -gt 1 ]; then
        printf 'Multiple serial ports found:\n%s\n\n' "$ports" >&2
        printf 'Choose one with: %s --port /dev/DEVICE\n' "$0" >&2
        exit 1
    fi
fi

if [ ! -e "$port" ]; then
    printf 'Serial port does not exist: %s\n' "$port" >&2
    exit 1
fi

"$firmware_dir/build.sh"
opp_load_idf
project_dir=$(opp_project_dir)
cd "$project_dir"

printf '\nFlashing %s\n' "$port"
opp_run_with_progress Flashing "$project_dir/build/flash.log" idf.py -p "$port" flash

# A self-built sensor has no label, and cannot show its pairing code on its own
# console before it is paired, because that console needs Wi-Fi that only arrives
# over the paired link. The bench is the one place the code is reachable.
printf '\nReading this sensor'"'"'s pairing code\n'
if ! "$IDF_PYTHON_ENV_PATH/bin/python" "$firmware_dir/read-pairing-code.py" "$port"; then
    printf 'Read it later with:\n  %s/read-pairing-code.py %s\n' "$firmware_dir" "$port" >&2
fi

if [ "$monitor" -eq 1 ]; then
    printf '\nOpening monitor on %s (exit with Ctrl-])\n' "$port"
    idf.py -p "$port" monitor
else
    printf '\nFlash complete. Rerun without --no-monitor to flash and monitor:\n  %s --port %s\n' "$0" "$port"
fi