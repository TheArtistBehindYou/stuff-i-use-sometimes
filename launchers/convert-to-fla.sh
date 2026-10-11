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
    for candidate in \
        "$DIR/tsproj2fla.py" \
        "$DIR/../dist/tsproj2fla.py" \
        "$DIR/../tsproj2fla.py"; do
        if [ -f "$candidate" ]; then
            printf '%s' "$candidate"
            return 0
        fi
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
    echo "command is not installed. Download tsproj2fla.py from the repo's dist/"
    echo "folder and place it beside this file."
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
