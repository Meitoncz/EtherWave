#!/usr/bin/env bash
set -euo pipefail

# Installs the built "EtherWave Client.app" into /Applications and sets up
# a LaunchAgent so it starts automatically at login, minimized to the menu
# bar tray (Open EtherWave / Pause stream / Resume stream / Close).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="EtherWave Client.app"
SRC_APP="$SCRIPT_DIR/dist/$APP_NAME"
DEST_APP="/Applications/$APP_NAME"
LAUNCH_AGENT_SRC="$SCRIPT_DIR/com.etherwave.client.plist"
LAUNCH_AGENT_DEST="$HOME/Library/LaunchAgents/com.etherwave.client.plist"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "This install script must run on macOS." >&2
    exit 1
fi

if [[ ! -d "$SRC_APP" ]]; then
    echo "Build it first: ./build.sh" >&2
    exit 1
fi

echo "==> Installing $APP_NAME to /Applications"
rm -rf "$DEST_APP"
cp -R "$SRC_APP" "$DEST_APP"

echo "==> Setting up autostart at login"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__APP_PATH__|$DEST_APP|g" "$LAUNCH_AGENT_SRC" > "$LAUNCH_AGENT_DEST"
launchctl unload "$LAUNCH_AGENT_DEST" 2>/dev/null || true
launchctl load "$LAUNCH_AGENT_DEST"

echo
echo "==> Done. EtherWave Client is installed and will start automatically at login."
echo "    To disable autostart: launchctl unload '$LAUNCH_AGENT_DEST' && rm '$LAUNCH_AGENT_DEST'"
echo "    To uninstall entirely: rm -rf '$DEST_APP'"
