#!/usr/bin/env bash
# tsproj2fla drag-and-drop launcher (macOS / Linux).
#
# Convert one or more ToonSquid .tsproj files to Adobe Animate .fla.
#
# Usage:
#   * Drag .tsproj files onto this file (in a file manager, or onto the macOS
#     Convert-to-FLA.command wrapper), or
#   * Run it from a terminal:  ./convert-to-fla.sh MyProject.tsproj [-o Out.fla]
#
# It looks for tsproj2fla.py beside itself, then in ../dist/, then on PATH.

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"

find_python() {
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

find_script() {
    # Accept tsproj2fla.py and near-misses such as tsproj2fla.py.txt (what you
    # get when a browser saves the raw script instead of downloading it).
    local dir candidate
    for dir in "$DIR" "$DIR/.." "$DIR/../dist"; do
        for candidate in "$dir"/tsproj2fla*.py*; do
            if [ -f "$candidate" ]; then
                printf '%s' "$candidate"
                return 0
            fi
        done
    done
    return 1
}

pause() {
    if [ -t 0 ]; then
        printf '\nPress Enter to close...'
        read -r _ || true
    fi
}

if [ "$#" -eq 0 ]; then
    echo "Drag a .tsproj file onto this launcher, or run:"
    echo "    $0 MyProject.tsproj"
    pause
    exit 1
fi

PY="$(find_python || true)"
SCRIPT="$(find_script || true)"

if [ -n "$SCRIPT" ]; then
    if [ -z "$PY" ]; then
        echo "Python 3 was not found. Install it from https://www.python.org/downloads/"
        pause
        exit 1
    fi
    RUN=("$PY" "$SCRIPT")
elif command -v tsproj2fla >/dev/null 2>&1; then
    RUN=(tsproj2fla)
else
    echo "Could not find tsproj2fla.py next to this launcher, and the 'tsproj2fla'"
    echo "command is not installed."
    echo
    echo "Grab the drag-and-drop bundle from the repo's dist/ folder:"
    echo "    tsproj2fla-drag-and-drop.zip"
    echo "Unzip it and keep tsproj2fla.py in the same folder as this file."
    echo
    echo "If you downloaded the raw tsproj2fla.py and it saved as"
    echo "tsproj2fla.py.txt, rename it to tsproj2fla.py."
    pause
    exit 1
fi

status=0
for file in "$@"; do
    echo "==> Converting: $file"
    "${RUN[@]}" "$file" || status=1
    echo
done

if [ "$status" -ne 0 ]; then
    echo "One or more conversions failed (see the messages above)."
fi

pause
exit "$status"
