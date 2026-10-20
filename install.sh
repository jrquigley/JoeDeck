#!/usr/bin/env bash
# JoeDeck installer for Raspberry Pi OS Lite (Bookworm or newer).
# Copyright (c) 2026 Joe Quigley, Big-N-Tasty Designs
#
#   sudo ./install.sh
#
# Everything it asks can also be given up front (handy for setting up many signs):
#   SIGNAGE_NAME="Lobby"                 name shown in the admin page
#   SIGNAGE_WIFI_SSID / SIGNAGE_WIFI_PASSWORD   Wi-Fi network to save on this sign
#   SIGNAGE_JOIN="signage-lobby.local"   join an existing sign network (any sign's address)
#   SIGNAGE_JOIN_USER="admin"            admin login of that network (password in SIGNAGE_PASSWORD)
#   SIGNAGE_PASSWORD="..."               admin password (new network) or the network's admin password (joining)
#   SIGNAGE_REBOOT=1                     reboot automatically at the end
#   SIGNAGE_PORT=80  SIGNAGE_ALLOW_MOUSE=1
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then echo "Please run with sudo:  sudo ./install.sh"; exit 1; fi
command -v apt-get >/dev/null || { echo "This installer needs Raspberry Pi OS (or another Debian-based system)."; exit 1; }
export DEBIAN_FRONTEND=noninteractive

SRC="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/signage
DATA=/var/lib/signage
BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot
UPDATE="${SIGNAGE_UPDATE:-0}"

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }

# --------------------------------------------------------------- who runs the sign
RUN_USER="${SUDO_USER:-}"
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
  if [ -d "$DATA" ]; then RUN_USER="$(stat -c %U "$DATA")"; fi      # updates run from the web page as root
fi
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
  echo "Run this from your normal user account with sudo (not as root)."; exit 1
fi
USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

# keep the port of an existing install
PORT="${SIGNAGE_PORT:-}"
if [ -z "$PORT" ] && [ -f /etc/systemd/system/signage-web.service ]; then
  PORT="$(grep -o -- '--port [0-9]*' /etc/systemd/system/signage-web.service | awk '{print $2}' | head -n1)"
fi
PORT="${PORT:-80}"

# --------------------------------------------------------------- questions (first install only)
NAME="${SIGNAGE_NAME:-}"
WIFI_SSID="${SIGNAGE_WIFI_SSID:-}"
WIFI_PW="${SIGNAGE_WIFI_PASSWORD:-}"
JOIN="${SIGNAGE_JOIN:-}"
JOIN_USER="${SIGNAGE_JOIN_USER:-admin}"
PW="${SIGNAGE_PASSWORD:-}"
FRESH=0; [ -f "$DATA/config.json" ] || FRESH=1
TTY=0
if [ "$UPDATE" != "1" ] && { exec 3</dev/tty; } 2>/dev/null; then TTY=1; fi   # works even via curl | sudo bash

ask()        { local __v; read -r -p "$2" __v <&3 || true; printf -v "$1" '%s' "$__v"; }
ask_secret() { local __v; read -r -s -p "$2" __v <&3 || true; echo; printf -v "$1" '%s' "$__v"; }

check_join() {   # verify the network login before spending minutes installing
  local code
  code="$(U="$JOIN_USER" P="$PW" python3 -c 'import json,os;print(json.dumps({"username":os.environ["U"],"password":os.environ["P"],"verify_only":True}))' \
    | curl -s -o /dev/null -w '%{http_code}' --max-time 15 -H 'Content-Type: application/json' --data @- "http://$JOIN/cluster/enroll" || true)"
  case "$code" in
    200) return 0 ;;
    403) echo "That admin username or password was not accepted by $JOIN."; return 1 ;;
    *)   echo "Couldn't reach a JoeDeck sign at $JOIN (is it switched on and on this network?)."; return 1 ;;
  esac
}

if [ "$FRESH" = "1" ] && [ "$UPDATE" != "1" ]; then
  if [ "$TTY" = "1" ]; then
    printf '\n\033[1mJoeDeck setup\033[0m  (press Enter to accept the [default])\n\n'
    [ -n "$NAME" ] || ask NAME "Name for this sign, e.g. Lobby [$(hostname)]: "
    if [ -z "${SIGNAGE_WIFI_SSID+x}" ]; then
      ask WIFI_SSID "Wi-Fi network for this sign (Enter to skip): "
      [ -z "$WIFI_SSID" ] || ask_secret WIFI_PW "Wi-Fi password: "
    fi
    if [ -z "${SIGNAGE_JOIN+x}" ]; then
      echo
      echo "Is this your FIRST sign? Just press Enter."
      echo "Adding to signs you already have? Type the address of any of them (e.g. signage-lobby.local)."
      ask JOIN "> "
    fi
    if [ -n "$JOIN" ]; then
      for try in 1 2 3; do
        [ -n "${SIGNAGE_JOIN_USER:-}" ] || { ask JOIN_USER "Admin username for your signs [admin]: "; JOIN_USER="${JOIN_USER:-admin}"; }
        [ -n "$PW" ] || ask_secret PW "Admin password for your signs: "
        check_join && break
        PW=""; [ "$try" = 3 ] && exit 1
      done
    else
      while [ -z "$PW" ]; do
        ask_secret PW "Choose an admin password (6+ characters): "
        ask_secret PW2 "Type it again: "
        if [ "${#PW}" -lt 6 ] || [ "$PW" != "$PW2" ]; then echo "Didn't match or too short. Try again."; PW=""; fi
      done
    fi
  else
    if [ -z "$PW" ]; then
      echo "No terminal to ask questions. Re-run with e.g.:  sudo SIGNAGE_PASSWORD='yourpassword' bash ..."; exit 1
    fi
    if [ -n "$JOIN" ]; then check_join || exit 1; fi
  fi
fi
[ "$TTY" = "1" ] && exec 3<&-

# --------------------------------------------------------------- packages
say "Installing packages (this takes a few minutes)"
apt-get update
apt-get install -y --no-install-recommends \
  python3-flask python3-pil python3-waitress cage curl fonts-dejavu-core
if ! apt-get install -y --no-install-recommends chromium; then
  apt-get install -y --no-install-recommends chromium-browser
fi

# --------------------------------------------------------------- app files
say "Copying app to $APP"
mkdir -p "$APP" "$DATA/media" "$DATA/thumbs"
rm -rf "$APP/static" "$APP/kiosk-ext"
cp -r "$SRC/app/." "$APP/"
chown -R root:root "$APP"                      # the web app can't modify its own code
chmod 755 "$APP/kiosk.sh" "$APP/server.py" "$APP/wifi_helper.py" "$APP/update.sh"
chown -R "$RUN_USER:$RUN_USER" "$DATA"
usermod -aG video,render,input,tty "$RUN_USER"

# --------------------------------------------------------------- no mouse pointer on the sign
RULE=/etc/udev/rules.d/99-signage-no-pointer.rules
mkdir -p /etc/udev/rules.d
if [ "${SIGNAGE_ALLOW_MOUSE:-0}" = "1" ]; then
  rm -f "$RULE"
else
  cat > "$RULE" <<'EOF'
# JoeDeck: a sign never needs a mouse. Hide pointing devices from the display so no
# cursor is drawn. Keyboards and touchscreens are not affected. Delete this file and
# reboot to use a mouse on the sign again.
ACTION=="add|change", SUBSYSTEM=="input", ENV{ID_INPUT_MOUSE}=="1", ENV{ID_INPUT_KEYBOARD}!="1", ENV{LIBINPUT_IGNORE_DEVICE}="1"
ACTION=="add|change", SUBSYSTEM=="input", ENV{ID_INPUT_TOUCHPAD}=="1", ENV{ID_INPUT_KEYBOARD}!="1", ENV{LIBINPUT_IGNORE_DEVICE}="1"
ACTION=="add|change", SUBSYSTEM=="input", ENV{ID_INPUT_POINTINGSTICK}=="1", ENV{ID_INPUT_KEYBOARD}!="1", ENV{LIBINPUT_IGNORE_DEVICE}="1"
ACTION=="add|change", SUBSYSTEM=="input", ENV{ID_INPUT_TABLET}=="1", ENV{ID_INPUT_KEYBOARD}!="1", ENV{LIBINPUT_IGNORE_DEVICE}="1"
EOF
fi
udevadm control --reload-rules || true
udevadm trigger --subsystem-match=input --action=change || true
python3 "$APP/make_blank_cursor.py" /usr/share/icons/signage-blank
for d in "$USER_HOME/.icons/default" "$USER_HOME/.local/share/icons/default"; do
  python3 "$APP/make_blank_cursor.py" "$d"
done
chown -R "$RUN_USER:$RUN_USER" "$USER_HOME/.icons" "$USER_HOME/.local"

# --------------------------------------------------------------- Wi-Fi
# Pi Wi-Fi power saving makes signs stop answering on the network; turn it off for every connection
mkdir -p /etc/NetworkManager/conf.d
printf '[connection]\nwifi.powersave = 2\n' > /etc/NetworkManager/conf.d/joedeck-wifi-powersave.conf
if [ -n "$WIFI_SSID" ]; then
  say "Saving Wi-Fi network \"$WIFI_SSID\""
  RES="$(S="$WIFI_SSID" P="$WIFI_PW" python3 -c 'import json,os;print(json.dumps({"cmd":"add","ssid":os.environ["S"],"password":os.environ["P"],"priority":20}))' | "$APP/wifi_helper.py")"
  case "$RES" in *'"ok": true'*) echo "Saved. The sign will use it whenever it's in range." ;; *) warn "Wi-Fi not saved: $RES" ;; esac
fi

# --------------------------------------------------------------- sign identity, login, network
systemctl stop signage-web.service 2>/dev/null || true
as_user() { sudo -u "$RUN_USER" env SIGNAGE_DATA="$DATA" "$@"; }
if [ -n "$NAME" ]; then as_user python3 "$APP/server.py" --port "$PORT" --name "$NAME"; fi
if [ "$FRESH" = "1" ] && [ -n "$JOIN" ]; then
  say "Joining your sign network through $JOIN"
  sudo -u "$RUN_USER" env SIGNAGE_DATA="$DATA" SIGNAGE_JOIN_PASSWORD="$PW" \
    python3 "$APP/server.py" --port "$PORT" --join "$JOIN" --join-user "$JOIN_USER" \
    || warn "Couldn't join now. Add this sign later from the Network page of any sign."
elif [ -n "$PW" ]; then
  as_user python3 "$APP/server.py" --port "$PORT" --set-password "$PW"
fi

# --------------------------------------------------------------- services
say "Installing services"
sed -e "s|@USER@|$RUN_USER|g" -e "s|@PORT@|$PORT|g" -e "s|@HOME@|$USER_HOME|g" "$SRC/systemd/signage-web.service"   > /etc/systemd/system/signage-web.service
sed -e "s|@USER@|$RUN_USER|g" -e "s|@PORT@|$PORT|g" -e "s|@HOME@|$USER_HOME|g" "$SRC/systemd/signage-kiosk.service" > /etc/systemd/system/signage-kiosk.service

cat > /etc/pam.d/signage-kiosk <<'EOF'
auth     required pam_unix.so nullok
account  required pam_unix.so
session  required pam_unix.so
session  required pam_systemd.so
EOF

# the web app may run exactly these as root, and nothing else
cat > /etc/sudoers.d/signage <<EOF
$RUN_USER ALL=(root) NOPASSWD: /sbin/reboot, /usr/sbin/reboot
$RUN_USER ALL=(root) NOPASSWD: $APP/wifi_helper.py ""
$RUN_USER ALL=(root) NOPASSWD: $APP/update.sh ""
EOF
chmod 440 /etc/sudoers.d/signage
visudo -cf /etc/sudoers.d/signage >/dev/null

systemctl daemon-reload
systemctl disable getty@tty1.service >/dev/null 2>&1 || true
systemctl enable signage-web.service signage-kiosk.service
systemctl set-default graphical.target

# --------------------------------------------------------------- quiet boot (no text on screen)
CMD="$BOOT/cmdline.txt"
if [ -f "$CMD" ]; then
  [ -f "$CMD.signage-backup" ] || cp "$CMD" "$CMD.signage-backup"
  line="$(head -n1 "$CMD")"
  line="${line//console=tty1/console=tty3}"
  for opt in quiet loglevel=3 logo.nologo vt.global_cursor_default=0 consoleblank=0; do
    case " $line " in *" $opt "*) ;; *) line="$line $opt" ;; esac
  done
  printf '%s\n' "$line" > "$CMD"
fi
CFG="$BOOT/config.txt"
if [ -f "$CFG" ]; then
  [ -f "$CFG.signage-backup" ] || cp "$CFG" "$CFG.signage-backup"
  grep -q '^disable_splash=1' "$CFG" || printf '\n# JoeDeck\ndisable_splash=1\n' >> "$CFG"
  grep -q '^dtoverlay=vc4-kms-v3d' "$CFG" || echo 'dtoverlay=vc4-kms-v3d' >> "$CFG"
fi

# --------------------------------------------------------------- start
systemctl restart signage-web.service
[ "$UPDATE" = "1" ] && { echo "Update installed."; exit 0; }

HOST="$(hostname)"
IP="$(hostname -I | awk '{print $1}')"
SUFFIX=""; [ "$PORT" = "80" ] || SUFFIX=":$PORT"
cat <<EOF

=====================================================================
 JoeDeck is installed.

 Manage your signs at:  http://$HOST.local$SUFFIX/   or   http://$IP$SUFFIX/
 (log in on ANY sign to manage all of them)

 The screen starts showing content after a restart.
=====================================================================
EOF
DO_REBOOT="${SIGNAGE_REBOOT:-}"
if [ -z "$DO_REBOOT" ] && { exec 3</dev/tty; } 2>/dev/null; then
  read -r -p "Restart now? [Y/n] " ans <&3 || true
  case "$ans" in [nN]*) DO_REBOOT=0 ;; *) DO_REBOOT=1 ;; esac
fi
if [ "$DO_REBOOT" = "1" ]; then echo "Restarting…"; sleep 1; systemctl reboot; fi
