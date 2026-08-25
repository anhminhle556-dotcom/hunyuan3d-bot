#!/usr/bin/env bash
set -euo pipefail

mkdir -p "${DATA_DIR:-/data}" /tmp/downloads
VNC_PASSWORD="${VNC_PASSWORD:-change-me}"
PORT="${PORT:-8080}"

x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null
Xvfb :99 -screen 0 1365x768x24 -ac +extension GLX +render -noreset &
sleep 1
fluxbox >/tmp/fluxbox.log 2>&1 &
x11vnc -display :99 -rfbauth /tmp/vncpass -forever -shared -rfbport 5900 -noxdamage >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc/ "$PORT" localhost:5900 >/tmp/novnc.log 2>&1 &

exec python /app/bot.py
