#!/bin/bash
# Starts the PictoChat host for the launchd service installed by install.sh. launchd starts it again when it exits.
cd "$(dirname "$0")"
set -a
# shellcheck disable=SC1091
. ./config.env
set +a

port="${PICTOCHAT_SERIAL:-}"
if [ -z "$port" ]; then
    port="$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)"
fi
if [ -z "$port" ] || [ ! -e "$port" ]; then
    echo "$(date '+%F %T') no ESP32-C3 found (${PICTOCHAT_SERIAL:-/dev/cu.usbmodem*}): plug it in; trying again" >&2
    sleep 15
    exit 1
fi
echo "$(date '+%F %T') starting the host on $port, API on ${PICTOCHAT_BIND:-0.0.0.0}:${PICTOCHAT_PORT:-7777}" >&2
# The token reaches the host through the PICTOCHAT_TOKEN environment variable, so it is not in the process list.
# Standard output (the host's protocol log) is discarded by the service; errors go to the log file.
exec ./venv/bin/python pictochat.py host --serial "$port" --offload \
    --channel "${PICTOCHAT_CHANNEL:-1}" --room "${PICTOCHAT_ROOM:-0}" --name "${PICTOCHAT_NAME:-Home}" \
    --gui-port "${PICTOCHAT_PORT:-7777}" --gui-bind "${PICTOCHAT_BIND:-0.0.0.0}" --out none
