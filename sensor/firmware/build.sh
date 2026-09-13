#!/bin/sh
set -eu

firmware_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=idf-env.sh
. "$firmware_dir/idf-env.sh"

opp_load_idf
project_dir=$(opp_project_dir)
cd "$project_dir"

if [ ! -f sdkconfig ] || ! grep -q '^CONFIG_IDF_TARGET="esp32c3"$' sdkconfig; then
    printf 'Configuring ESP-IDF target: esp32c3\n'
    idf.py set-target esp32c3
fi

printf 'Building Open Plant Pulse firmware with %s\n' "$(idf.py --version)"
idf.py build

printf '\nBuild complete: %s/build/open_plant_pulse.bin\n' "$project_dir"