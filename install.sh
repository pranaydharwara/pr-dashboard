#!/bin/bash
set -e

echo ""
echo "  PR Dashboard — Setup"
echo "  ====================="
echo ""

# Check dependencies
if ! command -v gh &> /dev/null; then
    echo "  ✗ GitHub CLI (gh) not found."
    echo "    Install it from https://cli.github.com/"
    exit 1
fi
echo "  ✓ GitHub CLI found"

if ! command -v python3 &> /dev/null; then
    echo "  ✗ Python 3 not found."
    exit 1
fi
echo "  ✓ Python 3 found"

if ! gh auth status &> /dev/null 2>&1; then
    echo ""
    echo "  ✗ GitHub CLI not authenticated."
    echo "    Run 'gh auth login' first."
    exit 1
fi
echo "  ✓ GitHub CLI authenticated"
echo ""

# Get repo
read -p "  GitHub repo to track (owner/repo): " REPO
if [ -z "$REPO" ]; then
    echo "  Error: repo is required (e.g. facebook/react)"
    exit 1
fi

# Validate repo exists
if ! gh repo view "$REPO" &> /dev/null 2>&1; then
    echo "  ✗ Could not access '$REPO'. Check the name and your permissions."
    exit 1
fi
echo "  ✓ Repo '$REPO' accessible"

# Get port
read -p "  Port [9847]: " PORT
PORT=${PORT:-9847}

# Write config
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cat > "$SCRIPT_DIR/config.json" << EOF
{
  "repo": "$REPO",
  "port": $PORT
}
EOF
echo ""
echo "  ✓ Config saved to config.json"

# macOS: background service + app
if [[ "$OSTYPE" == "darwin"* ]]; then
    PLIST_LABEL="com.prdashboard.server"
    PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
    PYTHON3_PATH="$(which python3)"
    APP="$HOME/Applications/PR Dashboard.app"
    APP_EXEC="$APP/Contents/MacOS/PR Dashboard"

    # Stop existing service if running
    launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true

    # One app bundle handles both jobs: launched with --serve by launchd it runs
    # the server, launched normally (Spotlight/Dock) it opens the dashboard.
    #
    # The executable is a compiled binary rather than a shell script on purpose.
    # A script runs as /bin/bash, so macOS would attribute the Accessibility
    # grant to bash instead of this app and the keystroke feature could never be
    # authorised. Compiling also pins the grant to this bundle rather than to a
    # versioned Homebrew python path that changes on upgrade.
    mkdir -p "$HOME/Applications"
    rm -rf "$APP" "$HOME/Applications/PR Dashboard Server.app"
    mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

    if ! cc -O2 -Wall -fobjc-arc -o "$APP_EXEC" "$SCRIPT_DIR/launcher.m" \
        -DPRD_PYTHON="\"$PYTHON3_PATH\"" \
        -DPRD_DIR="\"$SCRIPT_DIR\"" \
        -DPRD_URL="\"http://localhost:${PORT}\"" \
        -framework Cocoa -framework UserNotifications 2>/dev/null; then
        echo "  ✗ Could not compile the launcher (need Xcode Command Line Tools)."
        echo "    Run 'xcode-select --install' and re-run this script."
        exit 1
    fi

    # Build the .icns from the bundled icon.png
    if [ -f "$SCRIPT_DIR/icon.png" ]; then
        ICONSET="$(mktemp -d)/icon.iconset"
        mkdir -p "$ICONSET"
        for spec in "16 icon_16x16" "32 icon_16x16@2x" "32 icon_32x32" \
                    "64 icon_32x32@2x" "128 icon_128x128" "256 icon_128x128@2x" \
                    "256 icon_256x256" "512 icon_256x256@2x" "512 icon_512x512" \
                    "1024 icon_512x512@2x"; do
            sips -z "${spec% *}" "${spec% *}" "$SCRIPT_DIR/icon.png" \
                --out "$ICONSET/${spec#* }.png" >/dev/null 2>&1
        done
        iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/icon.icns" 2>/dev/null || true
        rm -rf "$(dirname "$ICONSET")"
    fi

    cat > "$APP/Contents/Info.plist" << APPPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>PR Dashboard</string>
    <key>CFBundleIdentifier</key>
    <string>com.prdashboard.app</string>
    <key>CFBundleName</key>
    <string>PR Dashboard</string>
    <key>CFBundleDisplayName</key>
    <string>PR Dashboard</string>
    <key>CFBundleIconFile</key>
    <string>icon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>PR Dashboard opens your linked Cursor chats by sending keystrokes to Cursor.</string>
</dict>
</plist>
APPPLIST

    # Use a stable designated requirement instead of the default ad-hoc cdhash.
    # That keeps Accessibility approval attached across launcher rebuilds.
    codesign --force --deep --sign - \
        --requirements '=designated => identifier "com.prdashboard.app"' \
        "$APP" 2>/dev/null || true

    # Create launchd agent (auto-starts on login, restarts on crash)
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${APP_EXEC}</string>
        <string>--serve</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/dashboard.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/dashboard.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
PLIST

    # Start the service now
    launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
    echo "  ✓ Background service installed (starts on login, restarts on crash)"
    echo "  ✓ Created ~/Applications/PR Dashboard.app"
    echo ""
    echo "  To use Cursor chat linking, enable \"PR Dashboard\" under"
    echo "  System Settings → Privacy & Security → Accessibility."
fi

echo ""
echo "  Setup complete! The server is already running."
echo ""
echo "  Open http://localhost:$PORT or launch PR Dashboard from Spotlight."
echo "  It starts automatically on login — no terminal needed."
echo ""
echo "  To stop:  launchctl bootout gui/\$(id -u)/com.prdashboard.server"
echo "  To start: launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.prdashboard.server.plist"
echo ""
