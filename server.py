#!/usr/bin/env python3
"""PR Dashboard — a local status page for your GitHub pull requests."""

import json
import os
import sqlite3
import subprocess
import sys
import signal
import socket
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config():
    if not CONFIG_PATH.exists():
        print(f"No config.json found. Run ./install.sh to set up, or create config.json manually.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    config["repo"] = os.environ.get("PR_DASHBOARD_REPO", config.get("repo", ""))
    config["port"] = int(os.environ.get("PR_DASHBOARD_PORT", config.get("port", 9847)))
    if not config["repo"]:
        print("Error: no repo configured. Set 'repo' in config.json or PR_DASHBOARD_REPO env var.")
        sys.exit(1)
    return config


CONFIG = load_config()
REPO = CONFIG["repo"]
PORT = CONFIG["port"]

PR_FIELDS = ",".join([
    "number", "title", "url", "state", "isDraft", "createdAt", "updatedAt",
    "headRefName", "baseRefName", "reviewDecision", "statusCheckRollup",
    "mergeable", "additions", "deletions", "changedFiles", "author",
    "reviewRequests",
])


def get_github_username():
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


GH_USERNAME = get_github_username()

CHAT_LINKS_PATH = Path(__file__).parent / "chat-links.json"


def _cursor_global_storage_dir():
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage"
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Cursor" / "User" / "globalStorage"
        return home / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage"
    return home / ".config" / "Cursor" / "User" / "globalStorage"


CURSOR_STATE_DB = _cursor_global_storage_dir() / "state.vscdb"
CURSOR_SEARCH_DB = _cursor_global_storage_dir() / "conversation-search.db"


def open_cursor_session(title):
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "Cursor" to activate
    delay 0.5
    tell application "System Events"
        keystroke "k" using command down
        delay 0.3
        keystroke "{safe_title}"
        delay 0.3
        key code 36
    end tell
    '''
    subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def load_chat_links():
    if CHAT_LINKS_PATH.exists():
        with open(CHAT_LINKS_PATH) as f:
            return json.load(f)
    return {}


def save_chat_links(links):
    with open(CHAT_LINKS_PATH, "w") as f:
        json.dump(links, f, indent=2)


def _ro_connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)


def _cursor_header_index():
    index = {}
    if not CURSOR_STATE_DB.exists():
        return index
    try:
        con = _ro_connect(CURSOR_STATE_DB)
    except sqlite3.Error:
        return index
    try:
        cur = con.cursor()
        try:
            rows = cur.execute(
                "SELECT composerId, isArchived, isSubagent, recency, value FROM composerHeaders"
            ).fetchall()
        except sqlite3.Error:
            return index
        for cid, archived, subagent, recency, value in rows:
            if archived or subagent:
                continue
            try:
                data = json.loads(value) if value else {}
            except (TypeError, ValueError):
                data = {}
            if data.get("isArchived") or data.get("isSubagent"):
                continue
            if data.get("isDraft") or data.get("isBestOfNSubcomposer"):
                continue
            ws = data.get("workspaceIdentifier") or {}
            uri = ws.get("uri") or {}
            workspace_path = uri.get("fsPath") or uri.get("path")
            name = (data.get("name") or "").strip()
            index[cid] = {
                "name": name,
                "workspace": workspace_path,
                "recency": recency or data.get("lastUpdatedAt") or data.get("createdAt") or 0,
            }
    finally:
        con.close()
    return index


def scan_cursor_sessions():
    headers = _cursor_header_index()
    sessions = []
    seen = set()

    if CURSOR_SEARCH_DB.exists():
        try:
            con = _ro_connect(CURSOR_SEARCH_DB)
        except sqlite3.Error:
            con = None
        if con is not None:
            try:
                cur = con.cursor()
                try:
                    rows = cur.execute(
                        "SELECT id, title, updated_at FROM conversations "
                        "WHERE COALESCE(is_archived, 0) = 0 AND title != '' "
                        "ORDER BY updated_at DESC"
                    ).fetchall()
                except sqlite3.Error:
                    rows = []
                for cid, title, updated_at in rows:
                    if not cid or not title:
                        continue
                    header = headers.get(cid, {})
                    sessions.append({
                        "id": cid,
                        "title": title,
                        "workspace": header.get("workspace"),
                        "url": f"cursor://session/{cid}",
                        "recency": header.get("recency") or updated_at or 0,
                    })
                    seen.add(cid)
            finally:
                con.close()

    for cid, header in headers.items():
        if cid in seen:
            continue
        name = header.get("name") or ""
        if not name:
            continue
        sessions.append({
            "id": cid,
            "title": name,
            "workspace": header.get("workspace"),
            "url": f"cursor://session/{cid}",
            "recency": header.get("recency") or 0,
        })

    sessions.sort(key=lambda s: s.get("recency") or 0, reverse=True)
    return sessions


WATCHES_PATH = Path(__file__).parent / "watches.json"


def load_watches():
    if WATCHES_PATH.exists():
        with open(WATCHES_PATH) as f:
            return json.load(f)
    return {}


def save_watches(watches):
    with open(WATCHES_PATH, "w") as f:
        json.dump(watches, f, indent=2)


def send_notification(title, body):
    script = f'display notification "{body}" with title "{title}"'
    subprocess.Popen(
        ["osascript", "-e", script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _classify_ci(pr):
    checks = pr.get("statusCheckRollup") or []
    for c in checks:
        if c.get("__typename") == "CheckRun" and c.get("conclusion") == "FAILURE":
            return "failing"
        if c.get("__typename") == "StatusContext" and c.get("state") == "FAILURE":
            return "failing"
    for c in checks:
        if c.get("__typename") == "CheckRun" and c.get("status") != "COMPLETED":
            return "pending"
        if c.get("__typename") == "StatusContext" and c.get("state") == "PENDING":
            return "pending"
    return "passing" if checks else "unknown"


def _pr_state(pr):
    return {
        "ci": _classify_ci(pr),
        "review": pr.get("reviewDecision", ""),
        "mergeable": pr.get("mergeable", ""),
        "title": pr.get("title", ""),
    }


def check_watches():
    watches = load_watches()
    if not watches:
        return
    prs = fetch_prs() or []
    review_prs = fetch_review_requests() or []
    all_prs = {str(pr["number"]): pr for pr in prs + review_prs}
    changed = False
    for pr_num, prev in list(watches.items()):
        pr = all_prs.get(pr_num)
        if not pr:
            continue
        curr = _pr_state(pr)
        title = curr["title"]
        short = f"#{pr_num} {title[:50]}"
        if prev.get("ci") in ("passing", "pending") and curr["ci"] == "failing":
            send_notification("CI Failed", short)
        elif prev.get("ci") == "failing" and curr["ci"] == "passing":
            send_notification("CI Passed", short)
        if prev.get("review") != "APPROVED" and curr["review"] == "APPROVED":
            send_notification("PR Approved", short)
        if prev.get("review") != "CHANGES_REQUESTED" and curr["review"] == "CHANGES_REQUESTED":
            send_notification("Changes Requested", short)
        if prev.get("mergeable") != "MERGEABLE" and curr["mergeable"] == "MERGEABLE" \
                and curr["review"] == "APPROVED" and curr["ci"] == "passing":
            send_notification("Ready to Merge", short)
        if curr != {k: prev.get(k) for k in curr}:
            watches[pr_num] = curr
            changed = True
    if changed:
        save_watches(watches)


def _watch_loop():
    while True:
        threading.Event().wait(300)
        try:
            check_watches()
        except Exception:
            pass


def fetch_prs():
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO, "--author", "@me",
         "--state", "open", "--json", PR_FIELDS, "--limit", "50"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def fetch_review_requests():
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO,
         "--search", "review-requested:@me state:open",
         "--state", "open", "--json", PR_FIELDS, "--limit", "50"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    prs = json.loads(result.stdout)
    if not GH_USERNAME:
        return prs
    return [
        pr for pr in prs
        if any(
            r.get("__typename") == "User" and r.get("login") == GH_USERNAME
            for r in pr.get("reviewRequests", [])
        )
    ]


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PR Dashboard</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔀</text></svg>">
<style>
  :root {
    --bg: #f8f9fb; --bg-card: #ffffff; --bg-hover: #f4f6fa;
    --text: #0f172a; --text2: #64748b; --text3: #94a3b8;
    --border: #e2e8f0;
    --shadow: 0 1px 3px rgba(15,23,42,0.06), 0 1px 2px rgba(15,23,42,0.04);
    --shadow-lg: 0 4px 12px rgba(15,23,42,0.08), 0 1px 3px rgba(15,23,42,0.06);
    --radius: 12px;
    --green: #16a34a; --green-bg: #ecfdf5; --green-border: #bbf7d0;
    --red: #dc2626; --red-bg: #fef2f2; --red-border: #fecaca;
    --yellow: #d97706; --yellow-bg: #fffbeb; --yellow-border: #fde68a;
    --blue: #2563eb; --blue-bg: #eff6ff; --blue-border: #bfdbfe;
    --purple: #7c3aed; --purple-bg: #f5f3ff; --purple-border: #ddd6fe;
    --gray-bg: #f1f5f9;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0c0f1a; --bg-card: #161b2e; --bg-hover: #1e2540;
      --text: #e2e8f0; --text2: #94a3b8; --text3: #64748b;
      --border: #1e293b;
      --shadow: 0 1px 3px rgba(0,0,0,0.3), 0 1px 2px rgba(0,0,0,0.2);
      --shadow-lg: 0 4px 12px rgba(0,0,0,0.4), 0 1px 3px rgba(0,0,0,0.3);
      --green: #4ade80; --green-bg: #052e16; --green-border: #166534;
      --red: #f87171; --red-bg: #350a0a; --red-border: #7f1d1d;
      --yellow: #fbbf24; --yellow-bg: #352008; --yellow-border: #78350f;
      --blue: #60a5fa; --blue-bg: #0c1e3a; --blue-border: #1e3a5f;
      --purple: #a78bfa; --purple-bg: #1e1040; --purple-border: #4c1d95;
      --gray-bg: #1e293b;
    }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif;
    color: var(--text); background: var(--bg);
    padding: 40px 32px; max-width: 1140px; margin: 0 auto;
    -webkit-font-smoothing: antialiased;
  }

  /* Nav */
  .nav { display: flex; gap: 4px; margin-bottom: 24px; }
  .nav a {
    padding: 7px 16px; border-radius: 8px; font-size: 13px; font-weight: 600;
    text-decoration: none; color: var(--text2); transition: all 0.15s;
  }
  .nav a:hover { background: var(--gray-bg); color: var(--text); }
  .nav a.active { background: var(--blue-bg); color: var(--blue); border: 1px solid var(--blue-border); }

  /* Header */
  .header { margin-bottom: 20px; }
  .header-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 2px; }
  h1 { font-size: 24px; font-weight: 800; letter-spacing: -0.5px; }
  .subtitle { font-size: 13px; color: var(--text3); font-weight: 500; }
  .refresh-info {
    font-size: 12px; color: var(--text3); display: flex; align-items: center; gap: 8px;
    font-weight: 500;
  }
  .live-dot {
    width: 7px; height: 7px; border-radius: 50%; background: var(--green);
    display: inline-block; box-shadow: 0 0 6px var(--green);
  }
  .refresh-info.loading .live-dot {
    background: var(--yellow); box-shadow: 0 0 6px var(--yellow);
    animation: pulse 1s infinite;
  }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
  .btn-refresh {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px;
    padding: 5px 12px; font-size: 12px; font-weight: 600; color: var(--text2);
    cursor: pointer; transition: all 0.15s;
  }
  .btn-refresh:hover { background: var(--bg-hover); border-color: var(--text3); }

  /* Stat cards */
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 32px; }
  .stat {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 20px; text-align: center; box-shadow: var(--shadow);
  }
  .stat .num {
    font-size: 36px; font-weight: 800; line-height: 1; letter-spacing: -1px;
    font-variant-numeric: tabular-nums;
  }
  .stat .label {
    font-size: 11px; font-weight: 600; color: var(--text3); margin-top: 6px;
    text-transform: uppercase; letter-spacing: 0.8px;
  }

  /* Table wrapper */
  .table-card {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: var(--radius); box-shadow: var(--shadow);
    overflow: hidden; margin-bottom: 20px;
  }

  /* Section divider */
  .section-row td {
    padding: 14px 20px 8px; border-bottom: 1px solid var(--border);
  }
  .section-label {
    display: inline-flex; align-items: center; gap: 8px;
    font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.8px;
  }
  .section-count {
    font-size: 10px; font-weight: 600; color: var(--text3);
    background: var(--gray-bg); border-radius: 10px; padding: 2px 7px;
  }

  /* Table */
  .pr-table { width: 100%; border-collapse: collapse; font-size: 13px; table-layout: fixed; }
  .pr-table th {
    text-align: left; padding: 12px 20px; font-size: 11px; font-weight: 600;
    color: var(--text3); text-transform: uppercase; letter-spacing: 0.5px;
    border-bottom: 1px solid var(--border); background: var(--bg-card);
  }
  .pr-table td {
    padding: 12px 20px; border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }
  .pr-table tbody tr:last-child td { border-bottom: none; }
  .pr-table tbody tr:hover td { background: var(--bg-hover); }

  /* PR info cell */
  .pr-title {
    font-weight: 600; font-size: 13px; color: var(--text); text-decoration: none;
    display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    line-height: 1.3;
  }
  .pr-title:hover { color: var(--blue); }
  .pr-meta {
    display: flex; align-items: center; gap: 6px; margin-top: 3px;
    font-size: 11px; color: var(--text3);
  }
  .pr-number { font-weight: 600; font-variant-numeric: tabular-nums; }
  .pr-branch {
    max-width: 180px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    font-family: 'SF Mono', 'Fira Code', monospace; font-size: 10.5px;
    background: var(--gray-bg); padding: 1px 6px; border-radius: 4px;
  }

  /* Badges */
  .badge {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 4px 10px; border-radius: 20px;
    font-size: 11px; font-weight: 600; white-space: nowrap;
    border: 1px solid transparent;
  }
  .badge-green { background: var(--green-bg); color: var(--green); border-color: var(--green-border); }
  .badge-red { background: var(--red-bg); color: var(--red); border-color: var(--red-border); }
  .badge-yellow { background: var(--yellow-bg); color: var(--yellow); border-color: var(--yellow-border); }
  .badge-blue { background: var(--blue-bg); color: var(--blue); border-color: var(--blue-border); }
  .badge-purple { background: var(--purple-bg); color: var(--purple); border-color: var(--purple-border); }
  .badge-gray { background: var(--gray-bg); color: var(--text3); }
  .badge-dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }

  .ci-fail-detail {
    font-size: 10.5px; color: var(--red); margin-top: 3px;
    line-height: 1.3; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }

  /* Size */
  .size-pill {
    display: inline-flex; align-items: center; gap: 5px; justify-content: flex-end;
    font-size: 11px; font-weight: 600; color: var(--text2);
    font-variant-numeric: tabular-nums;
  }
  .size-bar-track {
    width: 36px; height: 4px; background: var(--gray-bg); border-radius: 2px;
    overflow: hidden;
  }
  .size-bar-fill { height: 100%; border-radius: 2px; }

  /* Age */
  .age { font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .age-ok { color: var(--text2); }
  .age-stale { color: var(--yellow); }
  .age-old { color: var(--red); }

  /* Chat link */
  .chat-link {
    display: inline-flex; align-items: center; justify-content: center;
    width: 20px; height: 20px; border-radius: 6px; cursor: pointer;
    transition: all 0.15s; border: none; background: none; padding: 0;
    vertical-align: middle; position: relative;
  }
  .chat-link svg { width: 14px; height: 14px; }
  .chat-link.empty svg { color: var(--text3); opacity: 0.3; }
  .chat-link.empty:hover svg { opacity: 0.7; }
  .chat-link.linked svg { color: var(--blue); opacity: 0.8; }
  .chat-link.linked:hover svg { opacity: 1; }
  .chat-link-tooltip {
    display: none; position: absolute; bottom: calc(100% + 6px); left: 50%;
    transform: translateX(-50%); background: var(--text); color: var(--bg);
    font-size: 10px; font-weight: 500; padding: 4px 8px; border-radius: 6px;
    white-space: nowrap; z-index: 10; pointer-events: none;
  }
  .chat-link-tooltip::after {
    content: ''; position: absolute; top: 100%; left: 50%; transform: translateX(-50%);
    border: 4px solid transparent; border-top-color: var(--text);
  }
  .chat-link:hover .chat-link-tooltip { display: block; }

  /* Watch bell */
  .watch-btn {
    display: inline-flex; align-items: center; justify-content: center;
    width: 20px; height: 20px; border-radius: 6px; cursor: pointer;
    transition: all 0.15s; border: none; background: none; padding: 0;
    vertical-align: middle; position: relative;
  }
  .watch-btn svg { width: 13px; height: 13px; }
  .watch-btn.off svg { color: var(--text3); opacity: 0.3; }
  .watch-btn.off:hover svg { opacity: 0.7; }
  .watch-btn.on svg { color: var(--yellow); opacity: 0.9; }
  .watch-btn.on:hover svg { opacity: 1; }
  .watch-btn .chat-link-tooltip { display: none; position: absolute; bottom: calc(100% + 6px); left: 50%;
    transform: translateX(-50%); background: var(--text); color: var(--bg);
    font-size: 10px; font-weight: 500; padding: 4px 8px; border-radius: 6px;
    white-space: nowrap; z-index: 10; pointer-events: none;
  }
  .watch-btn .chat-link-tooltip::after {
    content: ''; position: absolute; top: 100%; left: 50%; transform: translateX(-50%);
    border: 4px solid transparent; border-top-color: var(--text);
  }
  .watch-btn:hover .chat-link-tooltip { display: block; }

  /* Chat link modal */
  .chat-modal-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 100;
    display: flex; align-items: center; justify-content: center;
    backdrop-filter: blur(2px);
  }
  .chat-modal {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 16px;
    padding: 24px; width: 420px; max-width: 90vw; box-shadow: var(--shadow-lg);
  }
  .chat-modal h3 {
    font-size: 15px; font-weight: 700; margin-bottom: 4px;
  }
  .chat-modal .modal-subtitle {
    font-size: 12px; color: var(--text3); margin-bottom: 16px;
  }
  .chat-modal input {
    width: 100%; padding: 10px 12px; border: 1px solid var(--border);
    border-radius: 8px; font-size: 13px; background: var(--bg);
    color: var(--text); outline: none; font-family: inherit;
  }
  .chat-modal input:focus { border-color: var(--blue); }
  .chat-modal input::placeholder { color: var(--text3); }
  .chat-modal-actions {
    display: flex; gap: 8px; margin-top: 14px; justify-content: flex-end;
  }
  .chat-modal-actions button {
    padding: 7px 16px; border-radius: 8px; font-size: 12px; font-weight: 600;
    cursor: pointer; border: 1px solid var(--border); transition: all 0.15s;
    font-family: inherit;
  }
  .btn-cancel { background: var(--bg); color: var(--text2); }
  .btn-cancel:hover { background: var(--bg-hover); }
  .btn-save { background: var(--blue); color: white; border-color: var(--blue); }
  .btn-save:hover { opacity: 0.9; }
  .btn-remove { background: var(--red-bg); color: var(--red); border-color: var(--red-border); }
  .btn-remove:hover { opacity: 0.9; }
  .session-item {
    padding: 10px 12px; border-radius: 8px; transition: background 0.1s;
    border-bottom: 1px solid var(--border);
  }
  .session-item:last-child { border-bottom: none; }
  .session-item:hover { background: var(--bg-hover); }

  /* Drag handle */
  .drag-handle {
    cursor: grab; color: var(--text3); font-size: 14px; user-select: none;
    padding: 0 4px; opacity: 0.4; transition: opacity 0.15s; line-height: 1;
    letter-spacing: 1px;
  }
  .pr-table tbody tr:hover .drag-handle { opacity: 0.8; }
  .drag-handle:active { cursor: grabbing; }
  .pr-table tbody tr.dragging { opacity: 0.4; }
  .pr-table tbody tr.drag-over-above td { box-shadow: inset 0 2px 0 var(--blue); }
  .pr-table tbody tr.drag-over-below td { box-shadow: inset 0 -2px 0 var(--blue); }

  /* Column widths */
  .pr-table th:nth-child(1), .pr-table td:nth-child(1) { width: 30px; padding: 12px 0 12px 14px; }
  .pr-table th:nth-child(2), .pr-table td:nth-child(2) { width: 34%; }
  .pr-table th:nth-child(3), .pr-table td:nth-child(3) { width: 11%; }
  .pr-table th:nth-child(4), .pr-table td:nth-child(4) { width: 22%; }
  .pr-table th:nth-child(5), .pr-table td:nth-child(5) { width: 9%; }
  .pr-table th:nth-child(6), .pr-table td:nth-child(6) { width: 6%; text-align: right; }
  .pr-table th:nth-child(7), .pr-table td:nth-child(7) { width: 8%; text-align: right; }

  /* Loading */
  #loading {
    text-align: center; padding: 100px 0; color: var(--text3); font-size: 14px;
    font-weight: 500;
  }
  .spinner {
    width: 24px; height: 24px; border: 3px solid var(--border);
    border-top-color: var(--blue); border-radius: 50%;
    animation: spin 0.8s linear infinite; margin: 0 auto 12px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
  <div class="header">
    <div class="header-top">
      <h1>PR Dashboard</h1>
      <div class="refresh-info" id="refresh-info">
        <span class="live-dot"></span>
        <span id="refresh-text">Loading...</span>
        <button class="btn-refresh" onclick="refresh()">Refresh</button>
      </div>
    </div>
    <div class="subtitle">__REPO__</div>
  </div>
  <nav class="nav">
    <a href="/" class="active">My PRs</a>
    <a href="/review">To Review</a>
  </nav>
  <div id="content"><div id="loading"><div class="spinner"></div>Fetching pull requests...</div></div>

<script>
const REFRESH_INTERVAL = 5 * 60 * 1000;
const isReviewPage = typeof PAGE_MODE !== 'undefined' && PAGE_MODE === 'review';

function ageDays(createdAt) {
  return Math.floor((Date.now() - new Date(createdAt).getTime()) / 86400000);
}

function sizeInfo(add, del) {
  const t = add + del;
  if (t <= 10) return ['XS', '#22c55e', 5];
  if (t <= 100) return ['S', '#22c55e', 15];
  if (t <= 300) return ['M', '#eab308', 35];
  if (t <= 1000) return ['L', '#f97316', 60];
  if (t <= 3000) return ['XL', '#dc2626', 85];
  return ['XXL', '#dc2626', 100];
}

function classifyCi(checks) {
  if (!checks || !checks.length) return ['unknown', []];
  const failures = [];
  for (const c of checks) {
    if (c.__typename === 'CheckRun' && c.conclusion === 'FAILURE') failures.push(c.name || 'unknown');
    else if (c.__typename === 'StatusContext' && c.state === 'FAILURE') failures.push(c.context || 'unknown');
  }
  if (failures.length) return ['failing', failures];
  const pending = checks.some(c =>
    (c.__typename === 'CheckRun' && c.status !== 'COMPLETED') ||
    (c.__typename === 'StatusContext' && c.state === 'PENDING')
  );
  if (pending) return ['pending', []];
  return ['passing', []];
}

function ciBadge(status, failures) {
  if (status === 'passing') return '<span class="badge badge-green"><span class="badge-dot" style="background:currentColor"></span>Passing</span>';
  if (status === 'pending') return '<span class="badge badge-blue"><span class="badge-dot" style="background:currentColor"></span>Running</span>';
  if (status === 'failing') {
    const short = failures.slice(0, 2).join(', ');
    const extra = failures.length > 2 ? ` +${failures.length - 2}` : '';
    return `<span class="badge badge-red"><span class="badge-dot" style="background:currentColor"></span>Failing</span>
      <div class="ci-fail-detail">${short}${extra}</div>`;
  }
  return '<span class="badge badge-gray">Unknown</span>';
}

function reviewBadge(pr) {
  if (pr.isDraft) return '<span class="badge badge-purple"><span class="badge-dot" style="background:currentColor"></span>Draft</span>';
  if (pr.reviewDecision === 'APPROVED') return '<span class="badge badge-green"><span class="badge-dot" style="background:currentColor"></span>Approved</span>';
  if (pr.reviewDecision === 'CHANGES_REQUESTED') return '<span class="badge badge-red"><span class="badge-dot" style="background:currentColor"></span>Changes</span>';
  return '<span class="badge badge-yellow"><span class="badge-dot" style="background:currentColor"></span>Awaiting</span>';
}

function mergeBadge(pr) {
  if (pr.mergeable === 'MERGEABLE') return '<span class="badge badge-green">Clean</span>';
  if (pr.mergeable === 'CONFLICTING') return '<span class="badge badge-red">Conflicts</span>';
  return '<span class="badge badge-gray">Unknown</span>';
}

function ageHtml(days) {
  const cls = days > 60 ? 'age age-old' : days > 30 ? 'age age-stale' : 'age age-ok';
  return `<span class="${cls}">${days}d</span>`;
}

function render(prs) {
  const approved = [], needsReview = [], drafts = [];
  for (const pr of prs) {
    const [ciStatus, ciFailures] = classifyCi(pr.statusCheckRollup);
    pr._ci = ciStatus; pr._ciF = ciFailures;
    pr._age = ageDays(pr.createdAt);
    const [sl, sc, sp] = sizeInfo(pr.additions, pr.deletions);
    pr._sizeLabel = sl; pr._sizeColor = sc; pr._sizePct = sp;
    pr._author = (pr.author && pr.author.login) || '';
    if (isReviewPage) {
      needsReview.push(pr);
    } else if (pr.isDraft) {
      drafts.push(pr);
    } else if (pr.reviewDecision === 'APPROVED') {
      approved.push(pr);
    } else {
      needsReview.push(pr);
    }
  }
  [approved, needsReview, drafts].forEach(a => a.sort((a, b) => a._age - b._age));

  const total = prs.length;
  const nApproved = approved.length;
  const nFailing = prs.filter(p => p._ci === 'failing').length;
  const nDraft = drafts.length;

  function watchBtnHtml(prNum) {
    const isOn = watchedPrs.has(String(prNum));
    const cls = isOn ? 'on' : 'off';
    const tip = isOn ? 'Watching' : 'Watch for changes';
    const svg = '<svg viewBox="0 0 24 24" fill="' + (isOn ? 'currentColor' : 'none') + '" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path><path d="M13.73 21a2 2 0 0 1-3.46 0"></path></svg>';
    return `<button class="watch-btn ${cls}" onclick="toggleWatch(event, ${prNum})" title="">
      ${svg}<span class="chat-link-tooltip">${tip}</span>
    </button>`;
  }

  function chatLinkHtml(prNum) {
    const link = chatLinks[String(prNum)];
    const svg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>';
    if (link) {
      return `<button class="chat-link linked" onclick="handleChatLink(event, ${prNum})" title="">
        ${svg}<span class="chat-link-tooltip">${link.title || 'Open chat'}</span>
      </button>`;
    }
    return `<button class="chat-link empty" onclick="handleChatLink(event, ${prNum})" title="">
      ${svg}<span class="chat-link-tooltip">Link a chat</span>
    </button>`;
  }

  function row(pr, section) {
    const branch = pr.headRefName || '';
    return `<tr draggable="true" data-pr="${pr.number}" data-section="${section}">
      <td><span class="drag-handle">&#8942;&#8942;</span></td>
      <td>
        <a href="${pr.url}" target="_blank" class="pr-title">${pr.title}</a>
        <div class="pr-meta">
          <span class="pr-number">#${pr.number}</span>
          ${watchBtnHtml(pr.number)}
          ${chatLinkHtml(pr.number)}
          ${isReviewPage && pr._author ? '<span class="pr-branch">' + pr._author + '</span>' : ''}
          <span class="pr-branch">${branch}</span>
        </div>
      </td>
      <td>${reviewBadge(pr)}</td>
      <td>${ciBadge(pr._ci, pr._ciF)}</td>
      <td>${mergeBadge(pr)}</td>
      <td style="text-align:right;">${ageHtml(pr._age)}</td>
      <td style="text-align:right;">
        <div class="size-pill">
          <div class="size-bar-track"><div class="size-bar-fill" style="width:${pr._sizePct}%;background:${pr._sizeColor};"></div></div>
          ${pr._sizeLabel}
        </div>
      </td>
    </tr>`;
  }

  function sectionRow(label, color, count) {
    return `<tr class="section-row"><td colspan="7">
      <span class="section-label" style="color:${color};">${label}</span>
      <span class="section-count">${count}</span>
    </td></tr>`;
  }

  function applySavedOrder(items, key) {
    const saved = JSON.parse(localStorage.getItem('pr-order-' + key) || '[]');
    if (!saved.length) return items;
    const map = new Map(items.map(p => [p.number, p]));
    const ordered = [];
    for (const num of saved) {
      if (map.has(num)) { ordered.push(map.get(num)); map.delete(num); }
    }
    for (const p of items) {
      if (map.has(p.number)) ordered.push(p);
    }
    return ordered;
  }

  approved.splice(0, approved.length, ...applySavedOrder(approved, 'approved'));
  needsReview.splice(0, needsReview.length, ...applySavedOrder(needsReview, 'needs_review'));
  drafts.splice(0, drafts.length, ...applySavedOrder(drafts, 'drafts'));

  let tableRows = '';
  if (isReviewPage) {
    if (needsReview.length) {
      tableRows += sectionRow('Awaiting your review', 'var(--yellow)', needsReview.length);
      tableRows += needsReview.map(p => row(p, 'needs_review')).join('');
    }
  } else {
    if (approved.length) {
      tableRows += sectionRow('Approved', 'var(--green)', nApproved);
      tableRows += approved.map(p => row(p, 'approved')).join('');
    }
    if (needsReview.length) {
      tableRows += sectionRow('Needs review', 'var(--yellow)', needsReview.length);
      tableRows += needsReview.map(p => row(p, 'needs_review')).join('');
    }
    if (drafts.length) {
      tableRows += sectionRow('Drafts', 'var(--purple)', nDraft);
      tableRows += drafts.map(p => row(p, 'drafts')).join('');
    }
  }

  document.getElementById('content').innerHTML = `
    <div class="stats">
      <div class="stat"><div class="num">${total}</div><div class="label">Open PRs</div></div>
      <div class="stat"><div class="num" style="color:var(--green);">${nApproved}</div><div class="label">Approved</div></div>
      <div class="stat"><div class="num" style="color:var(--red);">${nFailing}</div><div class="label">CI Failing</div></div>
      <div class="stat"><div class="num" style="color:var(--purple);">${nDraft}</div><div class="label">Drafts</div></div>
    </div>
    <div class="table-card">
      <table class="pr-table">
        <thead><tr>
          <th></th><th>Pull Request</th><th>Review</th><th>CI</th><th>Merge</th><th style="text-align:right;">Age</th><th style="text-align:right;">Size</th>
        </tr></thead>
        <tbody>${tableRows}</tbody>
      </table>
    </div>`;

  initDragAndDrop();
}

function initDragAndDrop() {
  let dragRow = null;
  const tbody = document.querySelector('.pr-table tbody');
  if (!tbody) return;

  tbody.addEventListener('dragstart', e => {
    const tr = e.target.closest('tr[draggable]');
    if (!tr) { e.preventDefault(); return; }
    dragRow = tr;
    tr.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', tr.dataset.pr);
  });

  tbody.addEventListener('dragend', e => {
    if (dragRow) dragRow.classList.remove('dragging');
    dragRow = null;
    tbody.querySelectorAll('.drag-over-above,.drag-over-below').forEach(
      el => el.classList.remove('drag-over-above', 'drag-over-below')
    );
  });

  tbody.addEventListener('dragover', e => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const tr = e.target.closest('tr[draggable]');
    if (!tr || !dragRow || tr === dragRow) return;
    if (tr.dataset.section !== dragRow.dataset.section) return;
    tbody.querySelectorAll('.drag-over-above,.drag-over-below').forEach(
      el => el.classList.remove('drag-over-above', 'drag-over-below')
    );
    const rect = tr.getBoundingClientRect();
    const mid = rect.top + rect.height / 2;
    if (e.clientY < mid) tr.classList.add('drag-over-above');
    else tr.classList.add('drag-over-below');
  });

  tbody.addEventListener('dragleave', e => {
    const tr = e.target.closest('tr[draggable]');
    if (tr) tr.classList.remove('drag-over-above', 'drag-over-below');
  });

  tbody.addEventListener('drop', e => {
    e.preventDefault();
    const tr = e.target.closest('tr[draggable]');
    if (!tr || !dragRow || tr === dragRow) return;
    if (tr.dataset.section !== dragRow.dataset.section) return;
    const rect = tr.getBoundingClientRect();
    const mid = rect.top + rect.height / 2;
    if (e.clientY < mid) tr.parentNode.insertBefore(dragRow, tr);
    else tr.parentNode.insertBefore(dragRow, tr.nextSibling);
    saveOrder(dragRow.dataset.section);
    tbody.querySelectorAll('.drag-over-above,.drag-over-below').forEach(
      el => el.classList.remove('drag-over-above', 'drag-over-below')
    );
  });
}

function saveOrder(section) {
  const rows = document.querySelectorAll(`tr[data-section="${section}"]`);
  const order = Array.from(rows).map(r => parseInt(r.dataset.pr));
  localStorage.setItem('pr-order-' + section, JSON.stringify(order));
}

let chatLinks = {};

async function loadChatLinks() {
  try {
    const res = await fetch('/api/chat-links');
    chatLinks = await res.json();
  } catch (e) { chatLinks = {}; }
}

let watchedPrs = new Set();

async function loadWatches() {
  try {
    const res = await fetch('/api/watches');
    const data = await res.json();
    watchedPrs = new Set(Object.keys(data));
  } catch (e) { watchedPrs = new Set(); }
}

async function toggleWatch(e, prNum) {
  e.stopPropagation();
  const key = String(prNum);
  const action = watchedPrs.has(key) ? 'remove' : 'add';
  try {
    await fetch('/api/watches', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pr: key, action}),
    });
    if (action === 'add') {
      watchedPrs.add(key);
      showToast('Watching #' + prNum);
    } else {
      watchedPrs.delete(key);
      showToast('Unwatched #' + prNum);
    }
    refresh();
  } catch (err) {
    showToast('Failed to update watch', true);
  }
}

function showToast(msg, isError) {
  const t = document.createElement('div');
  t.textContent = msg;
  Object.assign(t.style, {
    position: 'fixed', bottom: '20px', left: '50%', transform: 'translateX(-50%)',
    padding: '10px 20px', borderRadius: '8px', fontSize: '14px', zIndex: '10000',
    color: '#fff', background: isError ? '#e74c3c' : '#27ae60',
    boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
  });
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

function handleChatLink(e, prNum) {
  e.stopPropagation();
  const link = chatLinks[String(prNum)];
  if (link && !e.shiftKey) {
    if (link.url && link.url.startsWith('cursor://session/')) {
      fetch('/api/open-session', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: link.id || '', title: link.title}),
      }).then(r => r.json()).then(d => {
        if (!d.ok) showToast(d.error || 'Failed to open chat', true);
      });
      return;
    }
    window.open(link.url, '_blank');
    return;
  }
  showChatModal(prNum, link);
}

async function showChatModal(prNum, existing) {
  let sessions = [];
  try {
    const res = await fetch('/api/sessions');
    sessions = await res.json();
  } catch (e) {}

  const overlay = document.createElement('div');
  overlay.className = 'chat-modal-overlay';
  overlay.innerHTML = `
    <div class="chat-modal">
      <h3>${existing ? 'Edit' : 'Link'} chat for #${prNum}</h3>
      <div class="modal-subtitle">Search your Cursor chats or paste any URL</div>
      <input type="text" id="chat-search" placeholder="Search chats or paste a URL..." value="" autocomplete="off">
      <div id="session-list" style="max-height:240px;overflow-y:auto;margin-top:8px;"></div>
      <div class="chat-modal-actions">
        ${existing ? '<button class="btn-remove" id="chat-remove">Remove</button>' : ''}
        <button class="btn-cancel" id="chat-cancel">Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const input = document.getElementById('chat-search');
  const list = document.getElementById('session-list');

  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const wsName = s => {
    if (!s.workspace) return '';
    const parts = String(s.workspace).split('/').filter(Boolean);
    return parts.length ? parts[parts.length - 1] : '';
  };

  function renderSessions(query) {
    const q = query.toLowerCase().trim();
    const filtered = q
      ? sessions.filter(s => s.title.toLowerCase().includes(q) || (s.workspace || '').toLowerCase().includes(q))
      : sessions.slice(0, 15);
    if (!filtered.length && !q) {
      list.innerHTML = '<div style="padding:12px;font-size:12px;color:var(--text3);">No Cursor chats found</div>';
      return;
    }
    if (!filtered.length) {
      const isUrl = q.startsWith('http');
      list.innerHTML = isUrl
        ? `<div class="session-item" data-url="${esc(q)}" data-title="Custom link" style="cursor:pointer;">
            <div style="font-size:12px;font-weight:600;">Save as custom link</div>
            <div style="font-size:11px;color:var(--text3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(q)}</div>
          </div>`
        : '<div style="padding:12px;font-size:12px;color:var(--text3);">No matching chats</div>';
      return;
    }
    list.innerHTML = filtered.map(s => {
      const ws = wsName(s);
      const meta = ws ? `${ws} · ${s.id.slice(0, 8)}` : `${s.id.slice(0, 8)}`;
      return `
        <div class="session-item" data-id="${esc(s.id)}" data-url="${esc(s.url)}" data-title="${esc(s.title)}" style="cursor:pointer;">
          <div style="font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(s.title)}</div>
          <div style="font-size:10px;color:var(--text3);font-family:monospace;">${esc(meta)}</div>
        </div>`;
    }).join('');
  }

  renderSessions('');
  input.focus();

  input.addEventListener('input', () => renderSessions(input.value));
  input.addEventListener('keydown', e => {
    if (e.key === 'Escape') overlay.remove();
    if (e.key === 'Enter') {
      const val = input.value.trim();
      if (val.startsWith('http')) {
        saveChatLink(prNum, val, 'Custom link', '');
        overlay.remove();
      }
    }
  });

  list.addEventListener('click', e => {
    const item = e.target.closest('.session-item');
    if (!item) return;
    saveChatLink(prNum, item.dataset.url, item.dataset.title, item.dataset.id || '');
    overlay.remove();
  });

  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  document.getElementById('chat-cancel').onclick = () => overlay.remove();
  if (existing) {
    document.getElementById('chat-remove').onclick = async () => {
      await fetch('/api/chat-links', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({pr: String(prNum), action: 'remove'}),
      });
      await loadChatLinks();
      overlay.remove();
      refresh();
    };
  }
}

async function saveChatLink(prNum, url, title, id) {
  await fetch('/api/chat-links', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pr: String(prNum), url, title, id: id || ''}),
  });
  await loadChatLinks();
  refresh();
}

async function refresh() {
  const info = document.getElementById('refresh-info');
  const text = document.getElementById('refresh-text');
  info.classList.add('loading');
  text.textContent = 'Refreshing...';
  try {
    await Promise.all([loadChatLinks(), loadWatches()]);
    const res = await fetch(isReviewPage ? '/api/review' : '/api/prs');
    const prs = await res.json();
    render(prs);
    const now = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    text.textContent = `Updated ${now} · next in 5m`;
    info.classList.remove('loading');
  } catch (e) {
    text.textContent = 'Fetch failed — retrying in 5m';
    info.classList.remove('loading');
  }
}

refresh();
setInterval(refresh, REFRESH_INTERVAL);
</script>
</body>
</html>""".replace("__REPO__", REPO)


def build_review_html():
    h = DASHBOARD_HTML
    h = h.replace(
        '<a href="/" class="active">My PRs</a>',
        '<a href="/">My PRs</a>',
    ).replace(
        '<a href="/review">To Review</a>',
        '<a href="/review" class="active">To Review</a>',
    ).replace(
        '<title>PR Dashboard</title>',
        '<title>PR Dashboard - To Review</title>',
    ).replace(
        "const REFRESH_INTERVAL = 5 * 60 * 1000;",
        "const REFRESH_INTERVAL = 5 * 60 * 1000;\nconst PAGE_MODE = 'review';",
    )
    return h


REVIEW_HTML = build_review_html()


class Handler(BaseHTTPRequestHandler):
    def _json_response(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _html_response(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/prs":
            self._json_response(fetch_prs() or [])
        elif self.path == "/api/review":
            self._json_response(fetch_review_requests() or [])
        elif self.path == "/api/sessions":
            self._json_response(scan_cursor_sessions())
        elif self.path == "/api/chat-links":
            self._json_response(load_chat_links())
        elif self.path == "/api/watches":
            self._json_response(load_watches())
        elif self.path == "/review":
            self._html_response(REVIEW_HTML)
        elif self.path == "/" or self.path == "":
            self._html_response(DASHBOARD_HTML)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/chat-links":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            links = load_chat_links()
            if data.get("action") == "remove":
                links.pop(data["pr"], None)
            else:
                entry = {"url": data["url"], "title": data.get("title", "")}
                if data.get("id"):
                    entry["id"] = data["id"]
                links[data["pr"]] = entry
            save_chat_links(links)
            self._json_response({"ok": True})
        elif self.path == "/api/watches":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            watches = load_watches()
            pr_num = data["pr"]
            if data.get("action") == "remove":
                watches.pop(pr_num, None)
            else:
                prs = fetch_prs() or []
                review_prs = fetch_review_requests() or []
                pr = next((p for p in prs + review_prs if str(p["number"]) == pr_num), None)
                if pr:
                    watches[pr_num] = _pr_state(pr)
                else:
                    watches[pr_num] = {}
            save_watches(watches)
            self._json_response({"ok": True})
        elif self.path == "/api/open-session":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            title = data.get("title", "")
            session_id = data.get("id", "")
            if not title:
                self._json_response({"ok": False, "error": "No title provided"})
                return
            sessions = scan_cursor_sessions()
            match = None
            if session_id:
                match = next((s for s in sessions if s["id"] == session_id), None)
            if match is None:
                match = next(
                    (s for s in sessions if s["title"].lower() == title.lower()),
                    None,
                )
            if match is None:
                self._json_response({"ok": False, "error": f"Chat \"{title}\" not found"})
                return
            open_cursor_session(match["title"])
            self._json_response({"ok": True})
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    if port_in_use(PORT):
        print(f"Dashboard already running. Opening http://localhost:{PORT}")
        webbrowser.open(f"http://localhost:{PORT}")
        return

    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    watcher = threading.Thread(target=_watch_loop, daemon=True)
    watcher.start()

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"PR Dashboard running at http://localhost:{PORT}")
    webbrowser.open(f"http://localhost:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
