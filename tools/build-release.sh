#!/usr/bin/env bash
# Builds the download files into ./dist:
#   joedeck-install.sh   one self-contained installer (what the signs and your website use)
#   joedeck-<ver>.zip    the same files as a normal folder
#   SHA256SUMS.txt
set -euo pipefail
cd "$(dirname "$0")/.."
VER="$(python3 -c "import re;print(re.search(r'VERSION = \"([^\"]+)\"', open('app/cluster.py').read()).group(1))")"
rm -rf dist && mkdir -p dist/stage/joedeck
# everything the Pi needs (not .github / tools / dist)
cp -r app systemd install.sh README.md dist/stage/joedeck/
[ -f LICENSE ] && cp LICENSE dist/stage/joedeck/
find dist/stage -name __pycache__ -prune -exec rm -rf {} +
chmod 755 dist/stage/joedeck/install.sh dist/stage/joedeck/app/*.sh dist/stage/joedeck/app/server.py \
          dist/stage/joedeck/app/wifi_helper.py dist/stage/joedeck/app/make_blank_cursor.py
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='2026-01-01' -C dist/stage -czf dist/payload.tgz joedeck
{
cat <<HEAD
#!/usr/bin/env bash
# =====================================================================
#  JoeDeck $VER - self-contained installer
#  Copyright (c) 2026 Joe Quigley, Big-N-Tasty Designs
#  https://github.com/jrquigley/JoeDeck
#
#  On a Raspberry Pi (Raspberry Pi OS Lite):
#      curl -fsSL https://github.com/jrquigley/JoeDeck/releases/latest/download/joedeck-install.sh | sudo bash
#  or  sudo bash joedeck-install.sh
#
#  Unattended options (environment variables): SIGNAGE_NAME, SIGNAGE_WIFI_SSID,
#  SIGNAGE_WIFI_PASSWORD, SIGNAGE_JOIN, SIGNAGE_JOIN_USER, SIGNAGE_PASSWORD,
#  SIGNAGE_REBOOT=1, SIGNAGE_PORT, SIGNAGE_ALLOW_MOUSE=1.
#  SIGNAGE_EXTRACT_ONLY=DIR just unpacks the files.
# =====================================================================
set -euo pipefail
VERSION="$VER"
HEAD
cat <<'BODY'
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

base64 -d > "$TMP/payload.tgz" <<'__PI_SIGNAGE_PAYLOAD__'
BODY
base64 -w 76 dist/payload.tgz
cat <<'TAIL'
__PI_SIGNAGE_PAYLOAD__

if [ -n "${SIGNAGE_EXTRACT_ONLY:-}" ]; then
  mkdir -p "$SIGNAGE_EXTRACT_ONLY"
  tar -xzf "$TMP/payload.tgz" -C "$SIGNAGE_EXTRACT_ONLY"
  echo "JoeDeck $VERSION unpacked to $SIGNAGE_EXTRACT_ONLY/joedeck"
  exit 0
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo:  sudo bash joedeck-install.sh"; exit 1
fi
echo "JoeDeck $VERSION - Copyright (c) 2026 Joe Quigley, Big-N-Tasty Designs"
tar -xzf "$TMP/payload.tgz" -C "$TMP"
bash "$TMP/joedeck/install.sh"
TAIL
} > dist/joedeck-install.sh
chmod +x dist/joedeck-install.sh
(cd dist/stage && zip -qrX "../joedeck-$VER.zip" joedeck)
rm -rf dist/stage dist/payload.tgz
(cd dist && sha256sum joedeck-install.sh "joedeck-$VER.zip" > SHA256SUMS.txt)
echo "Built JoeDeck $VER:"; ls -l dist
