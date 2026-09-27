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

    # ESP-IDF's export script defines the toolchain paths in this shell. In a
    # terminal its twenty lines of activation chatter (printed on stderr) are
    # hidden; OPP_VERBOSE=1 shows them when activation needs debugging.
    # shellcheck disable=SC1090
    if [ -z "${OPP_VERBOSE:-}" ] && [ -t 1 ]; then
        . "$idf_path/export.sh" >/dev/null 2>&1
    else
        . "$idf_path/export.sh"
    fi

    if ! command -v idf.py >/dev/null 2>&1; then
        printf 'ESP-IDF activation completed, but idf.py is unavailable.\n' >&2
        printf 'Rerun with OPP_VERBOSE=1 to see the activation output.\n' >&2
        exit 1
    fi
}

opp_project_dir()
{
    CDPATH= cd -- "$(dirname -- "$0")/.." && pwd
}
# Run a command with its output folded into one progress bar.
#
# In a terminal, compile steps ([n/N] from ninja) and flash percentages
# (esptool's "(NN %)") redraw a single line, while warnings and errors still
# print in full. Everything goes to the log file as well. Outside a terminal,
# or with OPP_VERBOSE=1, the command's output is shown unchanged.
opp_run_with_progress()
{
    progress_label=$1
    progress_log=$2
    shift 2
    if [ -n "${OPP_VERBOSE:-}" ] || [ ! -t 1 ]; then
        "$@"
        return
    fi

    mkdir -p "$(dirname -- "$progress_log")"
    progress_status=$(mktemp "${TMPDIR:-/tmp}/opp-progress.XXXXXX")
    {
        if "$@" 2>&1; then
            printf '0' >"$progress_status"
        else
            printf '%s' "$?" >"$progress_status"
        fi
    } | tee "$progress_log" | awk -v label="$progress_label" '
        function bar(done, total,   filled, text, index_) {
            filled = total > 0 ? int(done * 30 / total) : 0
            text = ""
            for (index_ = 0; index_ < 30; index_++) {
                text = text (index_ < filled ? "#" : "-")
            }
            return "[" text "]"
        }
        function draw(text) {
            printf "\r\033[K%s", text
            fflush()
            current = text
        }
        BEGIN { draw(label " ...") }
        /warning:|error:|FAILED:|ninja: build stopped|A fatal error/ {
            printf "\r\033[K%s\n", $0
            draw(current)
            next
        }
        match($0, /^\[[0-9]+\/[0-9]+\]/) {
            split(substr($0, 2, RLENGTH - 2), step, "/")
            # Sub-builds such as the bootloader count their own steps; follow
            # only the largest count so the bar does not jump backwards.
            if (step[2] + 0 >= largest_total) {
                largest_total = step[2] + 0
                draw(sprintf("%s %s %d/%d", label, bar(step[1], step[2]), step[1], step[2]))
            }
            next
        }
        match($0, /\([0-9]+ %\)/) {
            percent = substr($0, RSTART + 1, RLENGTH - 4) + 0
            draw(sprintf("%s %s region %d %3d%%", label, bar(percent, 100), region + 1, percent))
            next
        }
        /^Wrote [0-9]+ bytes/ { region++ }
        END { printf "\n" }
    '
    progress_result=$(cat "$progress_status")
    rm -f "$progress_status"
    if [ "$progress_result" != 0 ]; then
        printf '\n%s failed (exit %s). Last lines of %s:\n' \
            "$progress_label" "$progress_result" "$progress_log" >&2
        tail -n 40 "$progress_log" >&2
        return "$progress_result"
    fi
    printf '%s done. Full output: %s\n' "$progress_label" "$progress_log"
}
