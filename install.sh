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
    SERVER_APP="$HOME/Applications/PR Dashboard Server.app"
    SERVER_APP_EXEC="$SERVER_APP/Contents/MacOS/PR Dashboard Server"

    # Stop existing service if running
    launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true

    # Build a .app bundle for the server so macOS Accessibility permission
    # attaches to a stable path (survives brew Python version bumps that
    # otherwise silently revoke the grant on the raw python binary).
    mkdir -p "$HOME/Applications"
    rm -rf "$SERVER_APP"
    mkdir -p "$SERVER_APP/Contents/MacOS"
    cat > "$SERVER_APP/Contents/Info.plist" << APPPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>PR Dashboard Server</string>
    <key>CFBundleIdentifier</key>
    <string>${PLIST_LABEL}</string>
    <key>CFBundleName</key>
    <string>PR Dashboard Server</string>
    <key>CFBundleDisplayName</key>
    <string>PR Dashboard Server</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSBackgroundOnly</key>
    <true/>
    <key>NSAppleEventsUsageDescription</key>
    <string>PR Dashboard opens your linked Cursor chats by sending keystrokes to Cursor.</string>
</dict>
</plist>
APPPLIST
    cat > "$SERVER_APP_EXEC" << RUNNER
#!/bin/bash
cd "${SCRIPT_DIR}"
exec "${PYTHON3_PATH}" "${SCRIPT_DIR}/server.py"
RUNNER
    chmod +x "$SERVER_APP_EXEC"

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
        <string>${SERVER_APP_EXEC}</string>
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
    echo "  ✓ Server app: $SERVER_APP"
    echo ""
    echo "  Grant Accessibility permission to \"PR Dashboard Server\" once"
    echo "  (System Settings → Privacy & Security → Accessibility) so it can"
    echo "  send keystrokes to Cursor when you click a linked chat."

    # Create macOS app that just opens the browser
    rm -rf "$HOME/Applications/PR Dashboard.app"
    osacompile -o "$HOME/Applications/PR Dashboard.app" \
        -e "open location \"http://localhost:${PORT}\""
    echo "  ✓ Created ~/Applications/PR Dashboard.app"
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
