# JoeDeck

Turn Raspberry Pis into digital signs you manage from any browser. Each sign boots straight to full-screen content, with no desktop, terminal or mouse pointer showing. Add one sign or fifty: they find each other on the network, and you can log in on any of them to manage all of them.

## Features

- **Images and websites.** Upload photos (they're auto-rotated and resized), or add web pages such as menus, schedules, dashboards and calendars, with zoom and auto-refresh.
- **Slideshows.** Set a display time for each item, choose fade, slide or instant transitions, and shuffle if you like. You can also show one item indefinitely.
- **Playlists + schedule.** Different playlists by day and time (e.g. *Breakfast menu Mon–Fri 06:00–11:00*), including overnight windows and date ranges.
- **Horizontal or vertical** screens, switchable from the web page.
- **One shared media library.** Upload once on any sign and use it on every sign. Each sign keeps its own copy, so it keeps playing if the network drops.
- **Groups in sync.** Put signs in a group (e.g. *Concourse*) and they show the same playlist, schedule or single item, changing images at the same moment.
- **Many signs, no central server.** Signs discover each other automatically. Everything is managed from one page, and the other signs keep running if one goes offline.
- **Users and permissions.** *Admins* manage everything. *Editors* manage only the signs you assign them.
- **Activity log.** See who changed what, and when, across all signs.
- **Wi-Fi settings.** Set Wi-Fi during install, change it later from the web page, or push a network to every sign at once.
- **One-click updates.** Signs check GitHub for new versions, and you update one sign or all of them with a click.

## Install

**1. Prepare the SD card** with [Raspberry Pi Imager](https://www.raspberrypi.com/software/):
- Device: your Pi model.
- OS: *Raspberry Pi OS (other) → Raspberry Pi OS Lite (64-bit)*.
- *Edit settings*: give each Pi a unique **hostname** (e.g. `signage-lobby`), a username and password, **your Wi‑Fi**, your **time zone**, and turn on **SSH** (under *Services*).

**2. Install.** Connect to the Pi with SSH (`ssh YOURUSER@signage-lobby.local`) and run:

```bash
curl -fsSL https://github.com/jrquigley/JoeDeck/releases/latest/download/joedeck-install.sh | sudo bash
```

It asks four quick questions:
1. A name for the sign (e.g. *Lobby*).
2. A Wi-Fi network to save (optional; press Enter to skip).
3. **First sign?** Press Enter and choose an admin password.
   **Adding to existing signs?** Type the address of any sign you already have (e.g. `signage-lobby.local`) and your admin login. The new sign joins automatically.
4. Restart now? Answer **Y**.

That's it. Open `http://signage-lobby.local` (or any sign's address) to manage your signs.

### Setting up lots of signs

Put the answers on the command line and nothing is asked:

```bash
curl -fsSL https://github.com/jrquigley/JoeDeck/releases/latest/download/joedeck-install.sh | \
  sudo SIGNAGE_NAME="Concourse 3" SIGNAGE_JOIN=signage-lobby.local SIGNAGE_PASSWORD='your-admin-password' \
       SIGNAGE_WIFI_SSID="Venue WiFi" SIGNAGE_WIFI_PASSWORD='wifi-password' SIGNAGE_REBOOT=1 bash
```

| Setting | Meaning |
|---|---|
| `SIGNAGE_NAME` | Name shown in the admin page |
| `SIGNAGE_WIFI_SSID`, `SIGNAGE_WIFI_PASSWORD` | Wi‑Fi network to save on this sign |
| `SIGNAGE_JOIN` | Address of an existing sign to join (leave out for the first sign) |
| `SIGNAGE_JOIN_USER` | Admin username for joining (default `admin`) |
| `SIGNAGE_PASSWORD` | New admin password (first sign), or your admin password (joining) |
| `SIGNAGE_REBOOT=1` | Restart automatically when done |
| `SIGNAGE_PORT` | Web port (default 80) |
| `SIGNAGE_ALLOW_MOUSE=1` | Don't hide mice and touchpads from the sign |

## Using it

- **Signs:** every sign with a live preview. Click one to manage it.
- **On a sign's page:**
  - **Display:** orientation, image fit, transition style and speed.
  - **Media library:** upload images or **+ Add website**. Zoom (150–200% is common on TVs) makes web text readable from across the room. *Reload every N min* keeps a page that stays on screen up to date.
  - **Playlists:** order, per-item time, shuffle, and which playlist is the default.
  - **Schedule:** switch playlists by day and time.
  - **Wi-Fi** (admins): see the connection, save networks, switch networks. A failed switch goes back to the old network automatically.
- **Groups:** make a group, tick its signs, then **Edit content** to build its playlists and schedule (or start from a copy of a sign's). Every sign in the group shows that content in lockstep. **Restart together** starts the loop from the top on all of them. Turn a group off, or take a sign out, and the sign goes back to its own content. A sign is in at most one group, and screen settings (orientation, fit, transitions) stay with each sign.
- **Activity:** the combined change log of all your signs.
- **Users** (admins): add admins and editors, and choose which signs each editor can manage.
- **Network** (admins):
  - Versions of every sign and **Update** / **Update all signs**.
  - **Wi-Fi for all signs**.
  - Add signs that were installed without joining.

### Websites: good to know

- Pages are shown live and keep running, so clocks, scores and rotating content all work.
- Sites that block being embedded are handled automatically on the signs. The admin page's *Preview* in your own browser may show those as blank, which is normal.
- Pages behind a login, or with cookie banners or pop-ups, may not look right. Pages built for TVs or kiosks work best.
- Websites need more memory than images. A Pi 3 or newer with 1 GB or more is recommended.

### Shared media library: good to know

- Everything you upload or add appears on every sign's library within seconds. Each sign copies the images in the background, and a card shows *copying…* until the copy has arrived.
- Deleting an item removes it from **every** sign and playlist. Only admins, or the person who added an item, can rename or delete it.
- Every sign stores every image (they're resized to at most 1920 px, usually well under 1 MB), so a 16 GB card holds thousands.

### Groups and lockstep: good to know

- Signs in a group work out what to show from their clocks, which stay in step through internet time sync, so they normally switch within a few hundredths of a second of each other. Each sign loads the next item a few seconds early, so the switch itself is instant.
- A sign that restarts or joins late jumps straight to the right item.
- Shuffle in a group uses the same "random" order on every sign.
- Scheduled playlist changes happen together, right as the minute ticks over.

## Updates

Signs check GitHub every few hours. When a new version is out, admins see a banner. Go to **Network → Update all signs** (or update one sign at a time). Each sign downloads the new version, installs it, keeps all content and settings, and restarts. Its screen is dark for a few minutes. Signs need internet access to update.

## Troubleshooting

| Problem | Fix |
|---|---|
| Sign shows *Offline* but is on | Discovery uses UDP port 47474. Keep signs on the same network/VLAN, or add the sign by address on the Network page. |
| *"Please log in again"* when managing another sign | The signs' clocks differ by more than 5 minutes. They need internet for time sync (`timedatectl`). |
| Forgot the admin password | On any sign: `sudo systemctl stop signage-web && sudo -u $USER SIGNAGE_DATA=/var/lib/signage python3 /opt/signage/server.py --set-password NEWPASS && sudo systemctl start signage-web`. The reset spreads to all signs. |
| Black screen if the TV is turned on after the Pi | Add `video=HDMI-A-1:1920x1080@60D` to `/boot/firmware/cmdline.txt`. |
| Wi-Fi page says it isn't available | It needs NetworkManager, which is standard on Raspberry Pi OS Bookworm and newer. |
| Need a mouse on a sign | Delete `/etc/udev/rules.d/99-signage-no-pointer.rules` and reboot. |
| Logs | `journalctl -u signage-web -f` (web/API), `journalctl -u signage-kiosk -f` (screen), `/var/lib/signage/update.log` (updates) |

Data lives in `/var/lib/signage`:
- `media/`: images
- `config.json`: this sign's content
- `cluster.json`: shared users and signs
- `node.json`: this sign's identity and network key (keep it private)
- `audit.jsonl`: activity log

## Security notes

JoeDeck is built for a trusted local network:
- Signs trust each other through a shared secret key, and every Pi-to-Pi request is signed.
- Passwords are stored as salted hashes. Wi‑Fi changes and updates run through two small, fixed helper programs, and nothing else gets root access.
- Traffic is plain HTTP, so don't expose signs directly to the internet. Use a VPN for remote access.

## For maintainers: publishing a new version

```bash
tools/release.sh 3.1 "What changed"
```

This sets the version, commits, tags, pushes, builds `joedeck-install.sh` and creates the GitHub release. Signs see the update shortly after. You can also create a release on GitHub's website with a tag like `v3.1` (it must match `VERSION` in `app/cluster.py`), and the included GitHub Action builds and attaches the download files. To build the files locally without releasing, run `tools/build-release.sh` (output goes to `dist/`).

---
© 2026 Joe Quigley, Big-N-Tasty Designs
