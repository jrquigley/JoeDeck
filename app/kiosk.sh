#!/bin/sh
# Started by cage (a minimal full-screen Wayland compositor). Waits for the
# web server, then runs Chromium full-screen on the player page.
PORT="${SIGNAGE_PORT:-80}"
URL="http://127.0.0.1:${PORT}/player"
APP_DIR="$(dirname "$0")"

i=0
while [ $i -lt 90 ]; do
  curl -fs -o /dev/null "http://127.0.0.1:${PORT}/healthz" && break
  i=$((i + 1)); sleep 1
done

# fresh browser profile on every start (no "restore pages?" prompts, no build-up of old data)
PROFILE="$HOME/.cache/signage-chromium"
rm -rf "$PROFILE"; mkdir -p "$PROFILE"

BROWSER="$(command -v chromium || command -v chromium-browser)"
exec "$BROWSER" \
  --kiosk "$URL" \
  --user-data-dir="$PROFILE" \
  --ozone-platform=wayland \
  --load-extension="$APP_DIR/kiosk-ext" \
  --disable-features=Translate,TranslateUI,MediaRouter,DisableLoadExtensionCommandLineSwitch \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --disable-session-crashed-bubble \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  --check-for-update-interval=31536000 \
  --password-store=basic \
  --disk-cache-size=52428800 \
  --autoplay-policy=no-user-gesture-required
