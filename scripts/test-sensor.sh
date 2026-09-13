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
    "$sensor_dir/firmware/components/sensor_protocol/sensor_protocol.c" \
    "$sensor_dir/firmware/components/bthome_payload/bthome_payload.c" \
    "$sensor_dir/firmware/components/sht45/sht45_decode.c" \
    "$sensor_dir/firmware/components/clock_policy/clock_policy.c" \
    "$sensor_dir/tests/test_protocols.c" \
    -o "$build_dir/test_protocols"

"$build_dir/test_protocols"
printf 'Protocol tests passed.\n'