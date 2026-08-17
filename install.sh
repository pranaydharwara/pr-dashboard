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

# macOS app
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo ""
    mkdir -p ~/Applications
    rm -rf "$HOME/Applications/PR Dashboard.app"
    osacompile -o "$HOME/Applications/PR Dashboard.app" \
        -e "do shell script \"cd '$SCRIPT_DIR' && python3 server.py &\""
    echo "  ✓ Created ~/Applications/PR Dashboard.app"
    echo "    Launch from Spotlight or Dock — no terminal needed."
fi

echo ""
echo "  Setup complete!"
echo ""
echo "  Start the dashboard:"
echo "    python3 $SCRIPT_DIR/server.py"
echo ""
echo "  It will open http://localhost:$PORT in your browser."
echo "  Auto-refreshes every 5 minutes. Two views:"
echo "    • My PRs    — your open pull requests"
echo "    • To Review — PRs where you're personally requested"
echo ""
