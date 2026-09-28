#!/bin/sh
# Install the hub as a macOS LaunchAgent that starts at login.
#
# Builds "Open Plant Pulse Hub.app" around a launcher that runs this checkout,
# signs it ad hoc so macOS can attribute Bluetooth permission to it, and loads
# the agent. Run it again after changing the launcher or moving the checkout;
# the first run asks for Bluetooth permission.
#
#   deploy/launchd/install.sh            install and (re)start the hub
#   deploy/launchd/install.sh --no-load  write the files only
#
# APP_DIR, AGENT_DIR and LOG_DIR override where the files go.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
PROJECT_DIR=${PROJECT_DIR:-$(cd "$HERE/../.." && pwd)}
APP_DIR=${APP_DIR:-"$HOME/Applications/Open Plant Pulse Hub.app"}
AGENT_DIR=${AGENT_DIR:-"$HOME/Library/LaunchAgents"}
LOG_DIR=${LOG_DIR:-"$HOME/Library/Logs/open-plant-pulse"}
LABEL=com.openplantpulse.hub
LOAD=1
[ "${1:-}" = "--no-load" ] && LOAD=0

if [ ! -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    echo "No virtual environment at $PROJECT_DIR/.venv; create it first (see docs/hub.md)." >&2
    exit 1
fi

render() {
    sed -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
        -e "s|@APP_DIR@|$APP_DIR|g" \
        -e "s|@LOG_DIR@|$LOG_DIR|g" "$1"
}

mkdir -p "$APP_DIR/Contents/MacOS" "$AGENT_DIR" "$LOG_DIR"
cp "$HERE/Info.plist" "$APP_DIR/Contents/Info.plist"
render "$HERE/hub.sh.in" > "$APP_DIR/Contents/MacOS/hub"
chmod 755 "$APP_DIR/Contents/MacOS/hub"
# Editing the launcher invalidates the old signature; without a valid one the
# scanner silently stays stopped.
codesign --force --sign - "$APP_DIR"
render "$HERE/$LABEL.plist.in" > "$AGENT_DIR/$LABEL.plist"

echo "Installed $APP_DIR"
echo "Installed $AGENT_DIR/$LABEL.plist"
[ "$LOAD" = 1 ] || exit 0

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AGENT_DIR/$LABEL.plist"
echo "Started $LABEL; the page is at http://localhost/"
