#!/usr/bin/env bash
# macOS drag-and-drop wrapper for tsproj2fla.
#
# Double-click it, or drag .tsproj files onto it in Finder. It opens in
# Terminal and delegates to convert-to-fla.sh beside it.
#
# (macOS runs a .command file in Terminal automatically, which keeps the
# window open so you can read any errors.)

DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/convert-to-fla.sh" "$@"
