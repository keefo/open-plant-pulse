#!/bin/sh
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build_dir="$root_dir/build/host-tests"
sensor_dir="$root_dir/sensor"

mkdir -p "$build_dir"
cc -std=c11 -Wall -Wextra -Werror \
    -I"$sensor_dir/firmware/components/sensor_protocol/include" \
    -I"$sensor_dir/firmware/components/bthome_payload/include" \
    -I"$sensor_dir/firmware/components/sht45/include" \
    -I"$sensor_dir/firmware/components/clock_policy/include" \
    -I"$sensor_dir/firmware/components/device_config_protocol/include" \
    -I"$sensor_dir/firmware/components/power_source/include" \
    -I"$sensor_dir/firmware/components/report_ack_protocol/include" \
    -I"$sensor_dir/firmware/components/wifi_credentials_protocol/include" \
    "$sensor_dir/firmware/components/sensor_protocol/sensor_protocol.c" \
    "$sensor_dir/firmware/components/bthome_payload/bthome_payload.c" \
    "$sensor_dir/firmware/components/sht45/sht45_decode.c" \
    "$sensor_dir/firmware/components/clock_policy/clock_policy.c" \
    "$sensor_dir/firmware/components/device_config_protocol/device_config_protocol.c" \
    "$sensor_dir/firmware/components/power_source/power_source.c" \
    "$sensor_dir/firmware/components/report_ack_protocol/report_ack_protocol.c" \
    "$sensor_dir/firmware/components/wifi_credentials_protocol/wifi_credentials_protocol.c" \
    "$sensor_dir/tests/test_protocols.c" \
    -o "$build_dir/test_protocols"

"$build_dir/test_protocols"
printf 'Protocol tests passed.\n'