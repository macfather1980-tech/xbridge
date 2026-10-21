#!/bin/sh
# XBridge installer for macOS.
#
# Sets up, in order:
#   1. Python virtualenv + dependencies (zeroconf, ifaddr)
#   2. ~/.xbridge/ config directory (writes the example config if empty)
#   3. launchd agent (auto arm/release on network change)   [skip: --no-launchd]
#   4. menu-bar app in ~/Applications/XBridge.app           [only with --gui]
#
# Requires: Xcode Command Line Tools (xcode-select --install)

set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
SERVICE="com.uaena.xbridge"
DOMAIN="gui/$(id -u)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

BUILD_GUI=0
SKIP_LAUNCHD=0
for arg in "$@"; do
    case "$arg" in
        --gui) BUILD_GUI=1 ;;
        --no-launchd) SKIP_LAUNCHD=1 ;;
        *) printf 'usage: install.sh [--gui] [--no-launchd]\n' >&2; exit 1 ;;
    esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 1. python
[ -x /usr/bin/python3 ] || die "python3 not found - install the Xcode Command Line Tools first:  xcode-select --install"

say "Python venv ($ROOT/.venv-xbridge)"
[ -d "$ROOT/.venv-xbridge" ] || /usr/bin/python3 -m venv "$ROOT/.venv-xbridge"
"$ROOT/.venv-xbridge/bin/python3" -m pip install --quiet --upgrade pip || true
# Tested combination (see THIRD-PARTY.md for licenses):
"$ROOT/.venv-xbridge/bin/python3" -m pip install --quiet "zeroconf==0.148.0" "ifaddr==0.2.0"
"$ROOT/.venv-xbridge/bin/python3" -c "import zeroconf, ifaddr; print('    deps ok: zeroconf %s, ifaddr %s' % (zeroconf.__version__, ifaddr.__version__))"

# ---------------------------------------------------------------- 2. config
say "Config directory ~/.xbridge"
mkdir -p "$HOME/.xbridge" "$HOME/Library/Logs"
if [ ! -f "$HOME/.xbridge/config.json" ]; then
    cp "$ROOT/config.example.json" "$HOME/.xbridge/config.json"
    echo "    wrote example config - replace it with your device via 'capture'"
fi

# --------------------------------------------------------------- 3. launchd
if [ "$SKIP_LAUNCHD" = "1" ]; then
    say "launchd agent: skipped (--no-launchd)"
else
    say "Installing launchd agent ($LAUNCH_AGENTS/$SERVICE.plist)"
    mkdir -p "$LAUNCH_AGENTS"
    sed -e "s|__XBRIDGE__|$ROOT|g" -e "s|__HOME__|$HOME|g" \
        "$ROOT/launchd/$SERVICE.plist.template" > "$LAUNCH_AGENTS/$SERVICE.plist"
    launchctl bootout "$DOMAIN/$SERVICE" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$LAUNCH_AGENTS/$SERVICE.plist"
    launchctl enable "$DOMAIN/$SERVICE"
    echo "    NOTE: this replaced any previously installed agent (same label)."
fi

# -------------------------------------------------------------------- 4. gui
if [ "$BUILD_GUI" = "1" ]; then
    [ -x /usr/bin/swiftc ] || die "swiftc not found - install the Xcode Command Line Tools first:  xcode-select --install"
    say "Building menu-bar app (~/Applications/XBridge.app)"
    APP="$HOME/Applications/XBridge.app"
    rm -rf "$APP"
    mkdir -p "$APP/Contents/MacOS"
    cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleExecutable</key>
	<string>XBridge</string>
	<key>CFBundleIdentifier</key>
	<string>com.uaena.xbridge</string>
	<key>CFBundleName</key>
	<string>XBridge</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>1.5</string>
	<key>LSUIElement</key>
	<true/>
	<key>LSMinimumSystemVersion</key>
	<string>13.0</string>
</dict>
</plist>
EOF
    swiftc -O "$ROOT/xbridge-gui.swift" -o "$APP/Contents/MacOS/XBridge"
    codesign --force -s - "$APP"
    echo "    built (ad-hoc signed - on a fresh Mac, right-click the app and choose 'Open' once)"
fi

say "Done."
cat <<EOF

Next steps:
  1. Pair your iPhone with Xcode once while it is on this Wi-Fi
     (over USB, or Wi-Fi pairing on iOS 17+).
  2. Install Tailscale on the Mac AND the iPhone, same account, VPN on.
  3. With the phone on your Wi-Fi, record its Bonjour identity:
       $ROOT/.venv-xbridge/bin/python3 $ROOT/xbridge.py capture
  4. Turn the bridge on (menu-bar icon, or):
       launchctl kickstart $DOMAIN/$SERVICE

EOF
