#!/bin/sh
# Stop the hub's LaunchAgent and remove it and its app bundle. The database in
# ~/.open-plant-pulse and the logs are left alone.
set -eu

APP_DIR=${APP_DIR:-"$HOME/Applications/Open Plant Pulse Hub.app"}
AGENT_DIR=${AGENT_DIR:-"$HOME/Library/LaunchAgents"}
LABEL=com.openplantpulse.hub

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$AGENT_DIR/$LABEL.plist"
rm -rf "$APP_DIR"
echo "Removed $LABEL"
