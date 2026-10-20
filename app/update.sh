#!/bin/bash
# JoeDeck updater (runs as root through a narrow sudo rule, takes no arguments from the web app).
# Downloads the latest release installer from GitHub, installs it (keeping all data), then reboots.
set -euo pipefail
APP=/opt/signage
STATUS=/var/lib/signage/update-status.json
LOG=/var/lib/signage/update.log

if [ "${1:-}" != "--run" ]; then
  # detach so the update survives the web server restarting during install
  exec systemd-run --unit="signage-update-$(date +%s)" --collect --quiet "$APP/update.sh" --run
fi

status() { printf '{"state":"%s","message":"%s","at":%s}\n' "$1" "$2" "$(date +%s)" > "$STATUS"; }
exec >>"$LOG" 2>&1
trap 'status failed "Update failed - see /var/lib/signage/update.log"' ERR

REPO="$(python3 -I -c 'import json;print(json.load(open("/opt/signage/release.json"))["repo"])')"
URL="https://github.com/$REPO/releases/latest/download/joedeck-install.sh"
TMP="$(mktemp)"
echo "=== $(date) updating from $URL"
status downloading "Downloading the latest version"
curl -fsSL --retry 3 --max-time 300 -o "$TMP" "$URL"
bash -n "$TMP"
grep -q "JoeDeck" "$TMP"
status installing "Installing (this takes a few minutes)"
SIGNAGE_UPDATE=1 bash "$TMP"
rm -f "$TMP"
status rebooting "Rebooting"
echo "=== $(date) done, rebooting"
sleep 2
systemctl reboot
