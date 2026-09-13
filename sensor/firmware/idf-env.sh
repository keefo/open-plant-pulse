#!/bin/sh

# Shared ESP-IDF environment setup for build.sh and flush.sh.

opp_load_idf()
{
    if command -v idf.py >/dev/null 2>&1; then
        return
    fi

    idf_path=${IDF_PATH:-"$HOME/esp/esp-idf"}
    if [ ! -f "$idf_path/export.sh" ]; then
        cat >&2 <<EOF
ESP-IDF was not found.

Install ESP-IDF 5.2 or newer, or clone the tested release:
  git clone --branch v5.5.5 --recursive https://github.com/espressif/esp-idf.git "$HOME/esp/esp-idf"
  "$HOME/esp/esp-idf/install.sh" esp32c3

Then rerun this script. To use another installation:
  IDF_PATH=/path/to/esp-idf $0
EOF
        exit 1
    fi

    if [ -d "$HOME/.espressif/python_env" ] && [ -z "${IDF_PYTHON_ENV_PATH:-}" ]; then
        IDF_PYTHON_ENV_PATH="$HOME/.espressif/python_env"
        export IDF_PYTHON_ENV_PATH
    fi

    # ESP-IDF's export script defines the toolchain paths in this shell.
    # shellcheck disable=SC1090
    . "$idf_path/export.sh"

    if ! command -v idf.py >/dev/null 2>&1; then
        printf 'ESP-IDF activation completed, but idf.py is unavailable.\n' >&2
        exit 1
    fi
}

opp_project_dir()
{
    CDPATH= cd -- "$(dirname -- "$0")/.." && pwd
}