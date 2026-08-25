#!/usr/bin/env bash
# Independent continuous X11 recorder for Hunyuan training.
# Deliberately does NOT use Playwright, Chromium DOM, asyncio, scrot-per-frame, or bot.py.
# One long-lived ffmpeg x11grab connection records JPEG frames. A watchdog restarts ffmpeg
# if WebGL/X11 causes it to exit. A separate sender loop streams the newest frame to Telegram.
set +e

DATA_DIR="${DATA_DIR:-/data}"
DISPLAY_NUM="${DISPLAY:-:99}"
SESSION_DIR="$DATA_DIR/training-v2/current"
OS_DIR="$SESSION_DIR/screens/os_timeline"
OS_LOG="$SESSION_DIR/os_timeline.jsonl"
HEALTH="$SESSION_DIR/recorder_health.json"
STREAM_MARKER="$SESSION_DIR/.generation_stream"
INTERVAL="${OS_CAPTURE_EVERY_SEC:-10}"
STREAM_EVERY="${OS_STREAM_EVERY_SEC:-20}"
MAX_FRAMES="${OS_MAX_FRAMES:-360}"
WIDTH="${OS_CAPTURE_WIDTH:-960}"

mkdir -p "$OS_DIR"

prune_redundant_storage() {
  # V2-V7 also produced Python-side timeline/generation PNGs. They are secondary copies and
  # can silently fill the 500MB Railway volume. Keep recent ones only; never delete action
  # before/after screenshots or Chrome profile/login data.
  for d in "$SESSION_DIR/screens/timeline" "$SESSION_DIR/screens/generation_frames"; do
    [ -d "$d" ] || continue
    count=$(find "$d" -maxdepth 1 -type f \( -name '*.png' -o -name '*.jpg' \) 2>/dev/null | wc -l | tr -d ' ')
    [ -z "$count" ] && count=0
    if [ "$count" -gt 120 ] 2>/dev/null; then
      excess=$((count - 120))
      find "$d" -maxdepth 1 -type f \( -name '*.png' -o -name '*.jpg' \) -printf '%T@ %p\n' 2>/dev/null \
        | sort -n | head -n "$excess" | cut -d' ' -f2- | while IFS= read -r old; do rm -f "$old"; done
    fi
  done
  jobs="$DATA_DIR/jobs"
  if [ -d "$jobs" ]; then
    find "$jobs" -maxdepth 1 -type f -name 'hunyuan-training-session-*.zip' -printf '%T@ %p\n' 2>/dev/null \
      | sort -nr | tail -n +6 | cut -d' ' -f2- | while IFS= read -r old; do rm -f "$old"; done
  fi
}

prune_redundant_storage

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

write_health() {
  local status="$1" last_epoch="$2" last_file="$3" restarts="$4" pid="$5" note="$6"
  mkdir -p "$SESSION_DIR"
  local tmp="$HEALTH.tmp.$$"
  printf '{"status":"%s","updated_at_epoch":%s,"last_frame_epoch":%s,"last_frame":"%s","ffmpeg_restarts":%s,"ffmpeg_pid":%s,"interval_sec":%s,"max_frames":%s,"note":"%s"}\n' \
    "$(json_escape "$status")" "$(date +%s)" "${last_epoch:-0}" "$(json_escape "$last_file")" \
    "${restarts:-0}" "${pid:-0}" "$INTERVAL" "$MAX_FRAMES" "$(json_escape "$note")" > "$tmp" 2>/dev/null
  mv -f "$tmp" "$HEALTH" 2>/dev/null
}

prune_frames() {
  # Keep disk usage bounded. Action screenshots are untouched; only continuous OS frames rotate.
  local count excess
  count=$(find "$OS_DIR" -maxdepth 1 -type f \( -name 'frame_*.jpg' -o -name 'frame_*.png' \) 2>/dev/null | wc -l | tr -d ' ')
  [ -z "$count" ] && count=0
  if [ "$count" -gt "$MAX_FRAMES" ] 2>/dev/null; then
    excess=$((count - MAX_FRAMES))
    find "$OS_DIR" -maxdepth 1 -type f \( -name 'frame_*.jpg' -o -name 'frame_*.png' \) -printf '%T@ %p\n' 2>/dev/null \
      | sort -n | head -n "$excess" | cut -d' ' -f2- | while IFS= read -r old; do rm -f "$old"; done
  fi
  # Old V6/V7 PNG frames are much larger. Keep at most 40 old PNGs.
  local png_count png_excess
  png_count=$(find "$OS_DIR" -maxdepth 1 -type f -name 'frame_*.png' 2>/dev/null | wc -l | tr -d ' ')
  [ -z "$png_count" ] && png_count=0
  if [ "$png_count" -gt 40 ] 2>/dev/null; then
    png_excess=$((png_count - 40))
    find "$OS_DIR" -maxdepth 1 -type f -name 'frame_*.png' -printf '%T@ %p\n' 2>/dev/null \
      | sort -n | head -n "$png_excess" | cut -d' ' -f2- | while IFS= read -r old; do rm -f "$old"; done
  fi
}

latest_frame() {
  find "$OS_DIR" -maxdepth 1 -type f \( -name 'frame_*.jpg' -o -name 'frame_*.png' \) -printf '%T@ %p\n' 2>/dev/null \
    | sort -nr | head -n 1 | cut -d' ' -f2-
}

# ---- Long-lived capture watchdog -------------------------------------------------
(
  restarts=0
  last_seen=""
  last_epoch=0
  ffpid=0
  while true; do
    mkdir -p "$OS_DIR"
    prune_frames

    # Start a single continuous x11grab process. Keeping the X connection open is more reliable
    # under Hunyuan WebGL than spawning scrot/ffmpeg anew for every frame.
    pattern="$OS_DIR/frame_%Y%m%dT%H%M%S.jpg"
    write_health "starting" "$last_epoch" "$last_seen" "$restarts" "0" "starting ffmpeg x11grab"

    ffmpeg -nostdin -hide_banner -loglevel error -y \
      -f x11grab -framerate "1/$INTERVAL" -video_size 1365x768 -i "$DISPLAY_NUM" \
      -vf "scale=${WIDTH}:-2:flags=fast_bilinear" -q:v 8 -an -f image2 -strftime 1 "$pattern" \
      >/tmp/hunyuan-screen-ffmpeg.out 2>/tmp/hunyuan-screen-ffmpeg.err &
    ffpid=$!
    write_health "running" "$last_epoch" "$last_seen" "$restarts" "$ffpid" "continuous ffmpeg recorder running"

    # While ffmpeg is alive, observe new files and log timestamps. This watcher never talks to DOM.
    while kill -0 "$ffpid" 2>/dev/null; do
      newest=$(latest_frame)
      if [ -n "$newest" ] && [ "$newest" != "$last_seen" ] && [ -s "$newest" ]; then
        last_seen="$newest"
        last_epoch=$(stat -c %Y "$newest" 2>/dev/null || date +%s)
        cp -f "$newest" "$OS_DIR/latest.jpg.tmp" 2>/dev/null
        mv -f "$OS_DIR/latest.jpg.tmp" "$OS_DIR/latest.jpg" 2>/dev/null
        stamp=$(date -u -d "@$last_epoch" +%Y%m%dT%H%M%SZ 2>/dev/null || date -u +%Y%m%dT%H%M%SZ)
        printf '{"captured_at_epoch":%s,"captured_at_utc":"%s","screenshot":"screens/os_timeline/%s","source":"ffmpeg_continuous_x11_v8"}\n' \
          "$last_epoch" "$stamp" "$(basename "$newest")" >> "$OS_LOG" 2>/dev/null
        write_health "running" "$last_epoch" "$last_seen" "$restarts" "$ffpid" "new frame captured"
        prune_frames
      fi
      sleep 2
    done

    wait "$ffpid" 2>/dev/null
    rc=$?
    restarts=$((restarts + 1))
    err=$(tail -c 500 /tmp/hunyuan-screen-ffmpeg.err 2>/dev/null | tr '\n' ' ')
    write_health "restarting" "$last_epoch" "$last_seen" "$restarts" "0" "ffmpeg exited rc=$rc $err"
    sleep 2
  done
) >/tmp/hunyuan-screen-watchdog.log 2>&1 &
WATCHDOG_PID=$!

# ---- Independent Telegram streamer ---------------------------------------------
(
  last_sent_file=""
  last_sent_at=0
  stale_warned=0
  while true; do
    sleep 3
    [ -f "$STREAM_MARKER" ] || { stale_warned=0; continue; }
    [ -n "${BOT_TOKEN:-}" ] || continue
    [ -n "${OWNER_ID:-}" ] || continue

    newest=$(latest_frame)
    [ -n "$newest" ] && [ -s "$newest" ] || continue
    now=$(date +%s)
    mtime=$(stat -c %Y "$newest" 2>/dev/null || echo 0)
    age=$((now - mtime))

    # If capture itself has stalled, tell the user once instead of silently stopping.
    if [ "$age" -gt $((INTERVAL * 3 + 10)) ]; then
      if [ "$stale_warned" -eq 0 ]; then
        curl -fsS --max-time 10 \
          -d "chat_id=${OWNER_ID}" \
          --data-urlencode "text=⚠️ مسجل الشاشة ما جاب فريم جديد منذ ${age}ث. المسجل الخارجي راح يعيد تشغيل ffmpeg تلقائياً؛ البوت ما يضغط أي شي بالموقع." \
          "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" >/dev/null 2>&1
        stale_warned=1
      fi
      continue
    else
      stale_warned=0
    fi

    if [ "$newest" != "$last_sent_file" ] && [ $((now - last_sent_at)) -ge "$STREAM_EVERY" ]; then
      started=$(python - "$STREAM_MARKER" 2>/dev/null <<'PY'
import json,sys
try:
    print(int(float(json.load(open(sys.argv[1], encoding='utf-8')).get('started_at', 0))))
except Exception:
    print(0)
PY
)
      [ -z "$started" ] && started=0
      elapsed=$((now - started)); [ "$elapsed" -lt 0 ] && elapsed=0
      sendtmp="/tmp/hunyuan_stream_${now}.jpg"
      cp -f "$newest" "$sendtmp" 2>/dev/null || continue
      curl -fsS --max-time 15 \
        -F "chat_id=${OWNER_ID}" \
        -F "photo=@${sendtmp}" \
        -F "caption=📸 Hunyuan — ${elapsed}ث من بدء التوليد | فريم خارجي مستقل V8" \
        "https://api.telegram.org/bot${BOT_TOKEN}/sendPhoto" >/dev/null 2>&1
      rc=$?
      rm -f "$sendtmp"
      if [ "$rc" -eq 0 ]; then
        last_sent_file="$newest"
        last_sent_at="$now"
      fi
    fi
  done
) >/tmp/hunyuan-screen-streamer.log 2>&1 &
STREAMER_PID=$!

printf '%s\n' "$WATCHDOG_PID" > /tmp/hunyuan-screen-watchdog.pid
printf '%s\n' "$STREAMER_PID" > /tmp/hunyuan-screen-streamer.pid

# Keep this supervisor alive. Child loops are themselves watchdogged.
while true; do
  sleep 60
done
