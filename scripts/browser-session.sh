#!/usr/bin/env bash
set -euo pipefail

ROOT=/data/provider-broker/browser
PROFILE="$ROOT/profile"
DISPLAY_NUMBER=:99
export HOME="$ROOT"
export XDG_CONFIG_HOME="$ROOT/config"
export XDG_CACHE_HOME="$ROOT/cache"
export XDG_RUNTIME_DIR="$ROOT/runtime"
mkdir -p "$PROFILE" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_RUNTIME_DIR"
chmod 700 "$ROOT" "$PROFILE" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_RUNTIME_DIR"

cleanup() {
  kill "${chrome_pid:-}" "${vnc_pid:-}" "${xvfb_pid:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

/usr/bin/Xvfb "$DISPLAY_NUMBER" -screen 0 1440x900x24 -nolisten tcp &
xvfb_pid=$!
sleep 1
/usr/bin/x11vnc -display "$DISPLAY_NUMBER" -localhost -forever -shared -nopw -rfbport 5900 &
vnc_pid=$!
DISPLAY="$DISPLAY_NUMBER" /usr/bin/google-chrome \
  --user-data-dir="$PROFILE" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9223 \
  --no-first-run \
  --no-default-browser-check \
  --password-store=basic \
  --disable-breakpad \
  --disable-gpu \
  --disable-dev-shm-usage \
  --disable-sync \
  about:blank &
chrome_pid=$!

wait "$chrome_pid"
