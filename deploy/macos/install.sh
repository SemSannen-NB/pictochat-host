#!/bin/bash
# Installs the PictoChat host as a background service on macOS (a launchd agent of the current user).
#
#   deploy/macos/install.sh              install or update, then start
#   deploy/macos/install.sh --no-start   install the files only
#   deploy/macos/install.sh --uninstall  stop and remove the service (the settings file is kept)
#
# The host is copied to ~/Library/Application Support/pictochat-host, because a background service may not read
# ~/Downloads, ~/Documents or ~/Desktop. Settings, including the token for Home Assistant, are in config.env there.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DIR="$HOME/Library/Application Support/pictochat-host"
LABEL="com.pictochat.host"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/pictochat-host.log"

if [ "${1:-}" = "--uninstall" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Service removed. Settings and the host's files are still in: $DIR"
    exit 0
fi

command -v python3 >/dev/null || { echo "python3 not found. Install it with: xcode-select --install" >&2; exit 1; }
mkdir -p "$DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$REPO/pictochat.py" "$REPO/deploy/macos/run.sh" "$DIR/"
chmod +x "$DIR/run.sh"
git -C "$REPO" rev-parse --short HEAD > "$DIR/pictochat.commit" 2>/dev/null || true
[ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install --quiet --disable-pip-version-check pyserial

if [ ! -f "$DIR/config.env" ]; then
    umask 077
    cat > "$DIR/config.env" <<CONF
# Settings of the PictoChat host service. After a change: deploy/macos/install.sh (or log out and in).
PICTOCHAT_TOKEN=$(openssl rand -hex 16)   # Home Assistant must send this token
PICTOCHAT_PORT=7777                       # API port Home Assistant connects to
PICTOCHAT_BIND=0.0.0.0                    # 0.0.0.0 = reachable from other machines and containers
PICTOCHAT_SERIAL=                         # the ESP32-C3's port; empty = the first /dev/cu.usbmodem*
PICTOCHAT_CHANNEL=1                       # Wi-Fi channel; keep it away from your own Wi-Fi's channel
PICTOCHAT_ROOM=0                          # 0-3 = Chat Room A-D
PICTOCHAT_NAME=Home                       # nickname shown on the DS (at most 10 characters)
CONF
fi

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>/bin/bash</string><string>$DIR/run.sh</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>15</integer>
    <key>StandardOutPath</key><string>/dev/null</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

if [ "${1:-}" != "--no-start" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "The PictoChat host runs as a service and starts again at every login."
fi
# shellcheck disable=SC1091
. "$DIR/config.env"
echo
echo "Enter this in Home Assistant (Settings > Devices & services > Add integration > PictoChat):"
echo "  Host:  host.docker.internal   (from a container on this Mac), or this Mac's address:"
for nic in en0 en1; do
    ip="$(ipconfig getifaddr "$nic" 2>/dev/null)" && echo "           $ip"
done
echo "  Port:  $PICTOCHAT_PORT"
echo "  Token: $PICTOCHAT_TOKEN"
echo
echo "Settings: $DIR/config.env"
echo "Log:      $LOG"
