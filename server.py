#!/usr/bin/env python3
"""PR Dashboard — a local status page for your GitHub pull requests."""

import json
import os
import subprocess
import sys
import signal
import socket
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

  function row(pr, section) {
    const branch = pr.headRefName || '';
    return `<tr draggable="true" data-pr="${pr.number}" data-section="${section}">
      <td><span class="drag-handle">&#8942;&#8942;</span></td>
      <td>
        <a href="${pr.url}" target="_blank" class="pr-title">${pr.title}</a>
        <div class="pr-meta">
          <span class="pr-number">#${pr.number}</span>
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

async function refresh() {
  const info = document.getElementById('refresh-info');
  const text = document.getElementById('refresh-text');
  info.classList.add('loading');
  text.textContent = 'Refreshing...';
  try {
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
    def do_GET(self):
        if self.path == "/api/prs":
            prs = fetch_prs()
            body = json.dumps(prs or []).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/review":
            prs = fetch_review_requests()
            body = json.dumps(prs or []).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/review":
            body = REVIEW_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/" or self.path == "":
            body = DASHBOARD_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
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

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"PR Dashboard running at http://localhost:{PORT}")
    webbrowser.open(f"http://localhost:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
