# PR Dashboard

A local status page for your GitHub pull requests. Zero dependencies beyond Python 3 and the GitHub CLI.

![Light mode](https://img.shields.io/badge/theme-light%20%2F%20dark-blue) ![Python 3](https://img.shields.io/badge/python-3.7%2B-blue)

## Features

- **My PRs** — all your open PRs with review status, CI, merge state, age, and size
- **To Review** — PRs where you or one of your GitHub teams is requested, with per-team filter tabs (Assigned to me, one per team, All)
- **Behind-base detection** — the Merge column calls out when a PR has fallen behind its base branch, not just whether it conflicts
- **Update from base** — merge `main` (or whatever the PR's base is) into a PR branch with one confirmed click. Creates a merge commit on the head branch on GitHub — never rebases, never merges the PR into main.
- **AI PR summaries** — on-demand structured explanations (purpose, blockers, requested changes, failing CI, next steps) via the Cursor Agent CLI, cached locally
- **Cursor chat integration** (macOS) — link PRs to Cursor chats and reopen them with one click
- **CI failure summaries** — see every failing check and open its details directly
- **Alert inbox + native notifications** (macOS) — watch PRs, keep a read/unread history, and click notifications to open the dashboard
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

The install script will:
1. Ask for your repo (e.g. `facebook/react`) and create a `config.json`
2. On macOS, build `PR Dashboard.app` into `~/Applications` — launch it from Spotlight to open the dashboard, and launchd runs the same binary in the background to serve it
3. On macOS, install that as a background service that starts on login and restarts on crash

The macOS app needs Xcode Command Line Tools to compile its launcher. If you don't have them, run `xcode-select --install` first.

After install, the server is already running — open `http://localhost:9847` or launch the app.

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

Then run manually:

```bash
python3 server.py
```

You can also override config with environment variables:

```bash
PR_DASHBOARD_REPO=facebook/react PR_DASHBOARD_PORT=8080 python3 server.py
```

## macOS Background Service

On macOS, the install script sets up a `launchd` agent so the dashboard:
- **Starts automatically** when you log in
- **Restarts on crash** — if the process dies, macOS brings it back
- **Runs silently** in the background with no terminal window

The same `PR Dashboard.app` hosts clickable native notifications in the background and opens your browser when launched from Spotlight or the Dock.

### Managing the service

```bash
# Stop the server
launchctl bootout gui/$(id -u)/com.prdashboard.server

# Start the server
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.prdashboard.server.plist

# Check if it's running
curl -s -o /dev/null -w "%{http_code}" http://localhost:9847
```

### Non-macOS

On Linux or WSL, run the server directly or add it to your init system:

```bash
python3 server.py &
```

## PR Row Actions

Status lives in the table columns — Review, CI (with failing check names), Merge, Age — so there's one list, not a summary plus a duplicate. The Merge column shows `Behind <base>` when a PR has drifted behind its base branch, `Conflicts` when it can't merge, and `Clean` otherwise. Two action buttons sit inline on each PR row:

- **Update from base** — appears when a PR's `mergeStateStatus` is `BEHIND`. Clicking asks for confirmation, then calls GitHub's `update-branch` API to merge the base branch (e.g. `main`) into the PR's head branch. It uses `expected_head_sha` so a stale click aborts rather than clobbering a newer commit. Cross-repo PRs are supported only when the maintainer-can-modify flag is on. The action never rebases and never merges the PR into main.
- **AI summary** — runs the installed [Cursor Agent CLI](https://cursor.com/cli) (`agent`) in read-only Ask mode against the PR's metadata, reviews, and diff (truncated at 60 KB). The result is a short markdown breakdown of purpose, blockers, requested changes, failing CI, and next steps. Summaries are cached in `ai-summaries.json` per PR; a stale-SHA badge appears if the PR head has moved since the summary was generated, and there's a Regenerate button. PRs never generate summaries automatically — the CLI runs only when you click.

Set `PR_DASHBOARD_AGENT=/path/to/agent` in the environment if the CLI lives somewhere unusual.

## Team Review Filters

The **To Review** page shows every open PR where either you or a team you belong to is on the reviewer list, with a compact filter row below the main nav:

- **Assigned to me** (default) — only PRs where you're personally requested. This mirrors the previous default behavior.
- One tab per team you belong to — PRs requested from that team. Tabs are generated from your actual GitHub memberships; nothing is hardcoded.
- **All** — everything the review search returned, regardless of who's assigned.

Each tab shows a live count. The current selection is remembered in `localStorage`, and if you leave a team the saved tab quietly falls back to *Assigned to me*. Empty states name the current filter so a blank list is obvious.

Team memberships come from `gh api user/teams`, filtered to the organization that owns your configured repo. So if `config.json` points at `your-org/your-repo`, only `your-org` teams become tabs — memberships in unrelated organizations are never fetched into the UI. Matching is done on GitHub's `org/team` slug, which is also what appears in a PR's team review requests.

This requires your CLI token to include the `read:org` scope. If your org is SAML-protected you may also need to authorize the token for it. If team loading fails, the direct filter keeps working and an inline error explains which auth step is missing — run `gh auth refresh -s read:org` (and follow any SSO prompt) if the team tabs don't appear.

## Cursor Chat Linking

On macOS with [Cursor](https://cursor.com) installed, you can link each PR to the Cursor chat where you worked on it. Click the chat icon on any PR row to search your Cursor chats and pick one. Clicking a linked icon activates Cursor, opens the Cmd+K conversation search, types the chat title, and hits Enter to jump into the top match. Shift-click to edit or remove a link.

Cursor has no official "open this chat" URL scheme yet, so the reopen step drives Cmd+K search via AppleScript. Two setup notes:

- **Cmd+K only searches chats inside the Agents Window.** In the classic editor window, Cmd+K opens the inline-edit prompt instead, and the title gets typed there. Open the Agents Window once (Cmd+Shift+P → "Open Agents Window") and keep it around — after that the flow works.
- **Enable "PR Dashboard" under System Settings → Privacy & Security → Accessibility.** The install script builds `~/Applications/PR Dashboard.app` as a small compiled launcher, so macOS attributes the permission to the app itself rather than to the Python binary behind it — the grant then survives Homebrew Python upgrades. If a click does nothing, the dashboard shows a toast naming the exact setting to flip.

Chat titles come from Cursor's local index (`state.vscdb` + `conversation-search.db` under `~/Library/Application Support/Cursor/User/globalStorage/`). The dashboard reads them read-only — nothing is written to Cursor's data. If a linked chat can no longer be found, you'll get an error toast instead of quietly landing in an unrelated chat.

The modal search also matches on workspace path, so typing a repo folder name narrows results to chats you had inside that project.

Chat link data is stored locally in `chat-links.json` (gitignored). You can also paste any `http(s)://` URL into the modal to save a custom link — handy for pointing a PR at a doc, ticket, or shared Cursor transcript URL.

## Notifications

Notifications are opt-in per PR — no noise by default. Click the bell icon on any PR row to start watching it. A background thread checks every 5 minutes and sends a native macOS notification when:

- **CI fails** — was passing or pending, now failing
- **CI passes** — was failing, now passing
- **PR approved** or **changes requested**
- **Ready to merge** — approved, CI green, and no conflicts

Clicking a native notification opens the dashboard alert inbox. The header bell shows the unread count; alerts can be marked read individually or all at once, and each alert links to its PR. CI failure alerts include the names of the failing checks.

Click the PR-row bell again to stop watching. Watch state is stored locally in `watches.json`; alert history is stored in `alerts.json` (both gitignored, capped at the latest 200 alerts).

## How It Works

- Runs a local HTTP server (Python's built-in `http.server`)
- Fetches PR data via the `gh` CLI on each refresh
- All data stays local — nothing is sent to any third-party service
- Port collision detection prevents duplicate servers

## License

MIT
