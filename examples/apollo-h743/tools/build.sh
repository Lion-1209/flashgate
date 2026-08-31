#!/usr/bin/env bash
# Build the Apollo firmware from the terminal using the STM32CubeIDE-bundled Ninja.
# Tries CUBE_BUNDLE_PATH first, then falls back to the standard Windows install path.
set -e

BUNDLE="${CUBE_BUNDLE_PATH:-$LOCALAPPDATA/stm32cube/bundles}"
NINJA=$(ls "$BUNDLE"/ninja/*/bin/ninja.exe 2>/dev/null | head -1)
if [ -z "$NINJA" ]; then
    echo "ERROR: bundled ninja not found under BUNDLE=$BUNDLE" >&2
    echo "       set CUBE_BUNDLE_PATH to your STM32Cube bundle directory." >&2
    exit 1
fi
exec "$NINJA" -C build/Debug "$@"
