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

x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null
Xvfb "$DISPLAY_NUM" -screen 0 1365x768x24 -ac +extension GLX +render -noreset &
sleep 1
DISPLAY="$DISPLAY_NUM" fluxbox >/tmp/fluxbox.log 2>&1 &
DISPLAY="$DISPLAY_NUM" x11vnc -display "$DISPLAY_NUM" -rfbauth /tmp/vncpass -forever -shared -rfbport 5900 -noxdamage >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc/ "$PORT" localhost:5900 >/tmp/novnc.log 2>&1 &

# OS-level sidecar recorder. This is a separate shell process, not asyncio, not Playwright,
# and not part of bot.py. Even if Chromium/WebGL or the Python event loop stalls, this loop
# continues taking screenshots from Xvfb every 5 seconds.
(
  export DISPLAY="$DISPLAY_NUM"
  last_sent=0
  while true; do
    mkdir -p "$OS_DIR"
    epoch_ms="$(date +%s%3N)"
    now_s="$(date +%s)"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    frame="$OS_DIR/frame_${epoch_ms}.png"
    tmp="/tmp/osframe_${epoch_ms}.png"
    ok=0

    if command -v scrot >/dev/null 2>&1; then
      if timeout 4s scrot -o "$tmp" >/dev/null 2>&1 && [ -s "$tmp" ]; then
        ok=1
      fi
    fi

    if [ "$ok" -ne 1 ] && command -v ffmpeg >/dev/null 2>&1; then
      rm -f "$tmp"
      if timeout 6s ffmpeg -loglevel error -y -f x11grab -video_size 1365x768 -i "$DISPLAY_NUM" -frames:v 1 "$tmp" >/dev/null 2>&1 && [ -s "$tmp" ]; then
        ok=1
      fi
    fi

    if [ "$ok" -eq 1 ]; then
      mv -f "$tmp" "$frame"
      cp -f "$frame" "$OS_DIR/latest.png"
      printf '{"captured_at_epoch":%s,"captured_at_utc":"%s","screenshot":"screens/os_timeline/%s","source":"os_sidecar_5s"}\n' \
        "$now_s" "$stamp" "$(basename "$frame")" >> "$OS_LOG"

      # During generation, also send a visible screenshot to Telegram every 15s.
      # This send is done by curl from this shell process, independent from bot.py.
      if [ -f "$STREAM_MARKER" ] && [ -n "${BOT_TOKEN:-}" ] && [ -n "${OWNER_ID:-}" ]; then
        if [ $((now_s - last_sent)) -ge 15 ]; then
          started="$(python - <<'PY' "$STREAM_MARKER" 2>/dev/null || true
import json,sys
try:
    d=json.load(open(sys.argv[1],encoding='utf-8'))
    print(int(float(d.get('started_at',0))))
except Exception:
    print(0)
PY
)"
          if [ -z "$started" ]; then started=0; fi
          elapsed=$((now_s - started))
          if [ "$elapsed" -lt 0 ]; then elapsed=0; fi
          # Safety: do not stream forever if a session is abandoned. After 30 minutes,
          # stop Telegram streaming but keep saving local OS frames.
          if [ "$elapsed" -gt 1800 ]; then
            rm -f "$STREAM_MARKER"
            sleep 5
            continue
          fi
          curl -fsS --max-time 12 \
            -F "chat_id=${OWNER_ID}" \
            -F "photo=@${frame}" \
            -F "caption=📸 Hunyuan — لقطة خارجية بعد ${elapsed}ث من بدء التوليد" \
            "https://api.telegram.org/bot${BOT_TOKEN}/sendPhoto" >/dev/null 2>&1 || true
          last_sent="$now_s"
        fi
      fi
    else
      rm -f "$tmp"
      printf '{"captured_at_epoch":%s,"captured_at_utc":"%s","screenshot":null,"source":"os_sidecar_5s","error":"capture_failed"}\n' \
        "$now_s" "$stamp" >> "$OS_LOG"
    fi
    sleep 5
  done
) >/tmp/os-sidecar.log 2>&1 &

exec python /app/bot.py
