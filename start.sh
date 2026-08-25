#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
VNC_PASSWORD="${VNC_PASSWORD:-change-me}"
PORT="${PORT:-8080}"
DISPLAY_NUM="${DISPLAY:-:99}"
SESSION_DIR="$DATA_DIR/training-v2/current"
OS_DIR="$SESSION_DIR/screens/os_timeline"
OS_LOG="$SESSION_DIR/os_timeline.jsonl"
STREAM_MARKER="$SESSION_DIR/.generation_stream"

mkdir -p "$DATA_DIR" /tmp/downloads "$OS_DIR"

# Railway volumes persist Chromium profile lock files across deploys.
# A crashed/old container can leave Singleton* behind and make the next deploy crash
# with "profile appears to be in use by another Chromium process".
# Remove ONLY process lock/control files; cookies/login/session data stay untouched.
PROFILE_DIR="$DATA_DIR/chrome-profile"
mkdir -p "$PROFILE_DIR"
rm -f \
  "$PROFILE_DIR/SingletonLock" \
  "$PROFILE_DIR/SingletonCookie" \
  "$PROFILE_DIR/SingletonSocket" \
  "$PROFILE_DIR/DevToolsActivePort" 2>/dev/null || true

# If this script was restarted inside the same container, stop a local stale Chromium
# that still points at this exact profile before Playwright launches a new one.
pkill -TERM -f "user-data-dir=$PROFILE_DIR" 2>/dev/null || true
sleep 1
pkill -KILL -f "user-data-dir=$PROFILE_DIR" 2>/dev/null || true
rm -f "$PROFILE_DIR/SingletonLock" "$PROFILE_DIR/SingletonCookie" "$PROFILE_DIR/SingletonSocket" "$PROFILE_DIR/DevToolsActivePort" 2>/dev/null || true

x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null
Xvfb "$DISPLAY_NUM" -screen 0 1365x768x24 -ac +extension GLX +render -noreset &
sleep 1
DISPLAY="$DISPLAY_NUM" fluxbox >/tmp/fluxbox.log 2>&1 &
DISPLAY="$DISPLAY_NUM" x11vnc -display "$DISPLAY_NUM" -rfbauth /tmp/vncpass -forever -shared -rfbport 5900 -noxdamage >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc/ "$PORT" localhost:5900 >/tmp/novnc.log 2>&1 &

# V8 recorder: one continuous ffmpeg X11 connection + independent watchdog/sender.
# It is a separate process from Python/Playwright and rotates JPEG frames to stay within the 500MB volume.
chmod +x /app/screen_recorder.sh
/app/screen_recorder.sh >/tmp/screen-recorder-supervisor.log 2>&1 &


exec python /app/bot.py
