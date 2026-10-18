#!/bin/bash
#
# Stremio Sidecar — installer
#
# Serves media files from this Mac to remote Stremio clients (Android TV, phone) over HTTPS
# with HTTP Range support, so playback has full seek. Reuses the Let's Encrypt certificate
# Stremio's own streaming server maintains, so the TV trusts it with no extra setup.
#
# Also installs a libtorrent-based torrent server that the TV uses as its Server URL in place of
# Stremio's own (whose single-threaded torrent engine stalls on big torrents).
#
#   ./install.sh
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.stremio-local-addon"
LABEL="com.local.stremio-library"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CERT="$HOME/Library/Application Support/stremio-server/httpsCert.json"
PORT=12471
TLABEL="com.local.stremio-torrent"
TPLIST="$HOME/Library/LaunchAgents/$TLABEL.plist"
TPORT=12472
LT_VERSION="2.1.1"
MENUBAR_PLUGIN="stremio-library.30s.sh"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

job_pid() { launchctl print "gui/$(id -u)/$1" 2>/dev/null | awk '$1 == "pid" {print $3; exit}'; }

# (Re)start a launch agent and wait until ITS process holds the port. `launchctl bootout` returns
# before the old process has exited, and a bootstrap issued meanwhile fails — while the old process
# still answers on the port, so checking the port alone would report success for a dead service.
restart_agent() { # label plist port log
  local label="$1" plist="$2" port="$3" log="$4" pid=""
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  for _ in $(seq 1 60); do
    launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 || break
    sleep 0.5
  done
  for _ in $(seq 1 20); do
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 0.5
  done
  launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null \
    || launchctl load -w "$plist" 2>/dev/null \
    || die "Could not load $label."
  for _ in $(seq 1 30); do
    pid="$(job_pid "$label")"
    if [ -n "$pid" ] && lsof -nP -a -p "$pid" -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  die "$label did not start listening on port $port. Check $log"
}

bold "Stremio Sidecar — installer"
echo

# ---------------------------------------------------------------- preflight
bold "Checking prerequisites"

[ "$(uname -s)" = "Darwin" ] || die "This installer is macOS only."
ok "macOS $(sw_vers -productVersion)"

if ! command -v node >/dev/null 2>&1; then
  warn "Node.js is not installed."
  if command -v brew >/dev/null 2>&1; then
    read -r -p "  Install it now with Homebrew? [y/N] " reply
    case "$reply" in
      [yY]*) brew install node ;;
      *) die "Node.js is required. Install it, then re-run this script." ;;
    esac
  else
    die "Node.js is required. Install Homebrew (https://brew.sh) then 'brew install node', or get Node from https://nodejs.org — then re-run."
  fi
fi

NODE="$(command -v node)"
NODE_VER="$(node -p 'process.versions.node')"
NODE_MAJOR="${NODE_VER%%.*}"
NODE_MINOR="$(echo "$NODE_VER" | cut -d. -f2)"
if [ "$NODE_MAJOR" -lt 22 ] || { [ "$NODE_MAJOR" -eq 22 ] && [ "$NODE_MINOR" -lt 5 ]; }; then
  die "Node 22.5 or newer is required (found $NODE_VER) — it provides the built-in sqlite module."
fi
ok "Node $NODE_VER at $NODE"

# The torrent server needs Python 3.9–3.13: libtorrent publishes prebuilt macOS wheels for those.
py_ok() { "$1" -c 'import sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] <= (3, 13) else 1)' 2>/dev/null; }
PYTHON=""
for p in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 \
         /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3; do
  if [ -x "$p" ] && py_ok "$p"; then PYTHON="$p"; break; fi
done
# Apple's /usr/bin/python3 is a stub that pops up an install dialog unless the Command Line Tools exist.
if [ -z "$PYTHON" ] && xcode-select -p >/dev/null 2>&1 && py_ok /usr/bin/python3; then
  PYTHON=/usr/bin/python3
fi
if [ -z "$PYTHON" ]; then
  warn "Python 3.9–3.13 is needed for the torrent server."
  if command -v brew >/dev/null 2>&1; then
    read -r -p "  Install Python 3.13 now with Homebrew? [y/N] " reply
    case "$reply" in
      [yY]*) brew install python@3.13 && PYTHON="$(brew --prefix)/bin/python3.13" ;;
      *) die "Python is required. Install it, then re-run this script." ;;
    esac
  else
    die "Python is required. Get Python 3.13 from https://www.python.org/downloads/ then re-run."
  fi
fi
ok "Python $("$PYTHON" -c 'import platform; print(platform.python_version())') at $PYTHON"

[ -d "/Applications/Stremio.app" ] || die "Stremio desktop is not installed in /Applications. Get it from https://www.stremio.com then re-run."
ok "Stremio desktop found"

if [ ! -f "$CERT" ]; then
  echo
  warn "Stremio has not yet issued its HTTPS certificate."
  echo "     Do this first, then re-run the installer:"
  echo "       1. Open Stremio desktop"
  echo "       2. Settings -> Streaming"
  echo "       3. Set 'Streaming HTTPS endpoint' to your Mac's LAN IP address"
  echo "       4. Wait a few seconds for it to fetch the certificate"
  exit 1
fi
CERT_DOMAIN="$(node -e "console.log(JSON.parse(require('fs').readFileSync('$CERT','utf8')).domain)")"
ok "HTTPS certificate present ($CERT_DOMAIN)"

# The cert is a wildcard for the install hash, so the hostname for THIS network is the live IP
# plus that hash — the cert's own domain string can still name a previous network's IP.
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
[ -n "$LAN_IP" ] || die "No LAN IP address — connect to your wifi, then re-run."
HOST="$(echo "$LAN_IP" | tr . -).${CERT_DOMAIN#*.}"

# ---------------------------------------------------------------- install
echo
bold "Installing"

mkdir -p "$INSTALL_DIR"
for f in addon.mjs autoheal.mjs auth.mjs cert.mjs advisor.mjs rating.mjs device-profile.default.json make-profile.mjs urls.sh \
         torrent_server.py stream_facts.py dashboard.py dashboard.html prefetch.py media_text.py page_model.py storage.py subtitles.py; do
  cp "$SRC_DIR/src/$f" "$INSTALL_DIR/$f"
done
chmod +x "$INSTALL_DIR/urls.sh"
ok "Files installed to $INSTALL_DIR"

# libtorrent lives in its own virtual environment, so nothing touches the system Python.
VENV="$INSTALL_DIR/venv"
if ! "$VENV/bin/python" -c "import libtorrent as lt; assert lt.__version__.startswith('$LT_VERSION')" 2>/dev/null; then
  rm -rf "$VENV"
  "$PYTHON" -m venv "$VENV" || die "Could not create a Python virtual environment in $VENV."
  "$VENV/bin/pip" install -q --disable-pip-version-check --only-binary=:all: "libtorrent==$LT_VERSION" \
    || die "Could not install libtorrent $LT_VERSION (prebuilt wheels need macOS 14+ on Apple Silicon or 15+ on Intel)."
fi
ok "libtorrent $("$VENV/bin/python" -c 'import libtorrent as lt; print(lt.__version__)') in $VENV"

# The addon scans these by default; create them so a fresh Mac has somewhere to drop media.
mkdir -p "$HOME/Media/Movies" "$HOME/Media/Shows" "$HOME/Media/Other"
ok "Media folders ready: Movies, Shows, Other"

# Seed the ignore list only if the user does not already have one.
if [ ! -f "$INSTALL_DIR/ignore.txt" ]; then
  cp "$SRC_DIR/templates/ignore.txt.example" "$INSTALL_DIR/ignore.txt"
  ok "Ignore list created at $INSTALL_DIR/ignore.txt"
else
  ok "Kept your existing $INSTALL_DIR/ignore.txt"
fi

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__NODE__|$NODE|g" -e "s|__DIR__|$INSTALL_DIR|g" \
  "$SRC_DIR/templates/launchagent.plist.template" > "$PLIST"
plutil -lint "$PLIST" >/dev/null || die "Generated launch agent is invalid."
ok "Launch agent written to $PLIST"
sed -e "s|__PYTHON__|$VENV/bin/python|g" -e "s|__DIR__|$INSTALL_DIR|g" \
  "$SRC_DIR/templates/torrent-launchagent.plist.template" > "$TPLIST"
plutil -lint "$TPLIST" >/dev/null || die "Generated torrent server launch agent is invalid."
ok "Launch agent written to $TPLIST"

# ---------------------------------------------------------------- menu bar
# Optional status icon, run by SwiftBar (https://swiftbar.app). Skipped cleanly if declined.
SWIFTBAR_APP=""
for d in /Applications "$HOME/Applications"; do
  [ -d "$d/SwiftBar.app" ] && SWIFTBAR_APP="$d/SwiftBar.app"
done
if [ -z "$SWIFTBAR_APP" ]; then
  if command -v brew >/dev/null 2>&1; then
    read -r -p "  Add a menu bar status icon? This installs SwiftBar with Homebrew. [Y/n] " reply
    case "$reply" in
      [nN]*) warn "Skipped the menu bar icon — re-run this installer any time to add it." ;;
      *) brew install --cask swiftbar && SWIFTBAR_APP="/Applications/SwiftBar.app" ;;
    esac
  else
    warn "Menu bar icon skipped — install SwiftBar from https://swiftbar.app then re-run to add it."
  fi
fi
if [ -n "$SWIFTBAR_APP" ]; then
  # Use the plugin folder SwiftBar already has; otherwise give it one, which also spares
  # the user its first-launch folder picker.
  PLUGIN_DIR="$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)"
  if [ -z "$PLUGIN_DIR" ] || [ ! -d "$PLUGIN_DIR" ]; then
    PLUGIN_DIR="$INSTALL_DIR/menubar"
    mkdir -p "$PLUGIN_DIR"
    defaults write com.ameba.SwiftBar PluginDirectory "$PLUGIN_DIR"
  fi
  cp "$SRC_DIR/src/menubar.30s.sh" "$PLUGIN_DIR/$MENUBAR_PLUGIN"
  chmod +x "$PLUGIN_DIR/$MENUBAR_PLUGIN"
  if pgrep -xq SwiftBar; then
    open -g "swiftbar://refreshallplugins"
  else
    open -g "$SWIFTBAR_APP"
  fi
  ok "Menu bar icon installed ($PLUGIN_DIR/$MENUBAR_PLUGIN)"
fi

# ---------------------------------------------------------------- start
echo
bold "Starting the service"

restart_agent "$LABEL" "$PLIST" "$PORT" "$INSTALL_DIR/addon.log"
ok "Listening on port $PORT"

# Give the self-heal a moment to register the addon on the Stremio account.
for _ in $(seq 1 20); do
  grep -q "autoheal:" "$INSTALL_DIR/addon.log" 2>/dev/null && break
  sleep 1
done

STATUS="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$HOST:$PORT/manifest.json" || true)"
[ "$STATUS" = "200" ] && ok "Addon responding over HTTPS" || warn "Addon did not answer over HTTPS yet (got '$STATUS')"

restart_agent "$TLABEL" "$TPLIST" "$TPORT" "$INSTALL_DIR/torrent.log"
STATUS="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$HOST:$TPORT/torrentd/health" || true)"
[ "$STATUS" = "200" ] && ok "Torrent server responding over HTTPS on port $TPORT" \
  || warn "Torrent server did not answer over HTTPS yet (got '$STATUS')"

# ---------------------------------------------------------------- done
echo
bold "Done"
echo
if grep -q "autoheal: INSTALLED\|autoheal: URL updated\|autoheal: account entry updated\|autoheal: account URL already correct" "$INSTALL_DIR/addon.log" 2>/dev/null; then
  echo "  The addon has been added to your Stremio account automatically."
  echo "  It will follow this Mac onto any network from now on."
else
  echo "  Sign in to Stremio desktop, then run:  $INSTALL_DIR/urls.sh"
  echo "  and paste the install URL under Addons."
fi
echo
echo "  On your TV:  Settings -> Server -> URL"
echo "      https://$HOST:$TPORT/"
echo "  (the libtorrent server; Stremio's own is still at :12470 if you ever want to switch back)"
echo
if grep -q "advisor: rating streams from" "$INSTALL_DIR/addon.log" 2>/dev/null; then
  echo "  Stream Advisor is on: every Torrentio stream also appears under \"Stream Advisor\", rated"
  echo "  1-5 for your TV. Uninstall Torrentio in Stremio to see each stream once (the advisor keeps"
  echo "  using it). Adapt the ratings to another TV in $INSTALL_DIR/device-profile.json."
  echo
fi
echo "  Whenever you change wifi, the URL changes. Click the menu bar icon to copy the current"
echo "  one, or run:  $INSTALL_DIR/urls.sh"
if [ -n "$SWIFTBAR_APP" ]; then
  echo
  echo "  To keep the menu bar icon after a reboot: SwiftBar -> Preferences -> Launch at Login."
fi
echo
echo "  Dashboard: http://localhost:11472/ui  (to open it on a phone, see \"Dashboard\" in the README)"
echo "  Logs:      tail -f $INSTALL_DIR/addon.log $INSTALL_DIR/torrent.log"
echo "  Uninstall: $SRC_DIR/uninstall.sh"
echo
