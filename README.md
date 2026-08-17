# PR Dashboard

A local status page for your GitHub pull requests. Zero dependencies beyond Python 3 and the GitHub CLI.

![Light mode](https://img.shields.io/badge/theme-light%20%2F%20dark-blue) ![Python 3](https://img.shields.io/badge/python-3.7%2B-blue)

## Features

- **My PRs** — all your open PRs with review status, CI, merge state, age, and size
- **To Review** — PRs where you're personally requested as a reviewer (filters out team-only requests)
- Auto-refreshes every 5 minutes
- Drag-and-drop to prioritize PRs within each section (saved to browser localStorage)
- Light/dark mode follows your system preference
- Single Python file, no pip install needed

## Quick Start

### Prerequisites

- [Python 3.7+](https://www.python.org/)
- [GitHub CLI (`gh`)](https://cli.github.com/) — installed and authenticated (`gh auth login`)

### Setup

```bash
git clone https://github.com/pranaydharwara/pr-dashboard.git
cd pr-dashboard
chmod +x install.sh
./install.sh
```

The install script will ask for your repo (e.g. `facebook/react`), create a `config.json`, and on macOS automatically create a `PR Dashboard.app` in `~/Applications`.

### Run

```bash
python3 server.py
```

Opens `http://localhost:9847` in your browser automatically.

### Manual config

If you prefer to skip the install script, copy the example config and edit it:

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
  "repo": "your-org/your-repo",
  "port": 9847
}
```

You can also override config with environment variables:

```bash
PR_DASHBOARD_REPO=facebook/react PR_DASHBOARD_PORT=8080 python3 server.py
```

## macOS App

On macOS, the install script automatically creates a `PR Dashboard.app` in `~/Applications` so you can launch it from Spotlight or the Dock without opening a terminal.

## How It Works

- Runs a local HTTP server (Python's built-in `http.server`)
- Fetches PR data via the `gh` CLI on each refresh
- All data stays local — nothing is sent to any third-party service
- Port collision detection prevents duplicate servers

## License

MIT
