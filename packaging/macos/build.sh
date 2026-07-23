#!/usr/bin/env bash
set -euo pipefail

# Builds "EtherWave Client.app": generates the icon, converts it to .icns,
# and runs PyInstaller against EtherWaveClient.spec. Run from anywhere;
# paths are resolved relative to this script's location.
#
# Requirements: macOS with Xcode command line tools (for sips/iconutil),
# Python 3.10+.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ASSETS_DIR="$PROJECT_ROOT/assets"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "This build script must run on macOS (needs sips/iconutil for the .icns icon)." >&2
    exit 1
fi

echo "==> Installing build dependencies"
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet -r "$PROJECT_ROOT/requirements.txt" pyinstaller Pillow

echo "==> Generating icon.png"
python3 "$ASSETS_DIR/generate_icon.py"

echo "==> Converting icon.png to icon.icns"
ICONSET="$ASSETS_DIR/icon.iconset"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$ASSETS_DIR/icon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" "$ASSETS_DIR/icon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$ASSETS_DIR/icon.icns"
rm -rf "$ICONSET"

echo "==> Running PyInstaller"
cd "$SCRIPT_DIR"
pyinstaller EtherWaveClient.spec --noconfirm --distpath "$SCRIPT_DIR/dist" --workpath "$SCRIPT_DIR/build"

echo
echo "==> Done: $SCRIPT_DIR/dist/EtherWave Client.app"
echo "    Run ./install.sh to copy it to /Applications and set up autostart at login."
