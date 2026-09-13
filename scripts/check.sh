#!/bin/sh
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root_dir"

python3 scripts/validate-bom.py
sh scripts/test-sensor.sh
PYTHONPATH=hub/src python3 -m unittest discover -s hub/tests -v
git diff --check