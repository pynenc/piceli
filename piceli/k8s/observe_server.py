"""Piceli Operator Server: unified versioned REST API and semantic reactive operator UI.

Library, CLI, versioned REST, and UI share identical authorization and operation semantics.
Single-instance state store and file-based backup ensure reliability without distributed consensus.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from piceli.artifacts.gc import ImageSpaceEntry, ImageSpaceInventory, SafeGarbageCollector
from piceli.k8s.automation import (
    ApprovalStore,
    GitBranchWatcher,
    PRApproval,
    dependency_safe_partial_release,
    health_aware_rollback,
    promote_release,
)
from piceli.k8s.observe import (
    ForwardSupervisor,
    InventoryReport,
    KNOWN_SHORTCUTS,
    PortForward,
    PreferenceStore,
    kubectl_logs_command,
)
from piceli.k8s.operator import OperatorReport, redact_log_content
from piceli.k8s.operator_state import FileStateStore, PolicyStore, UserStore
from piceli.k8s.release import ReleaseCatalog, ReleaseWorkflow


_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Piceli Operator — Piceli Observe</title>
  <style>
    :root {
      --bg: #090d13;
      --surface: #121820;
      --card: #1c2331;
      --card-hover: #232c3d;
      --border: #2d3748;
      --text: #e2e8f0;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --accent-hover: #0ea5e9;
      --success: #22c55e;
      --success-glow: rgba(34, 197, 94, 0.25);
      --warning: #eab308;
      --danger: #ef4444;
      --purple: #a855f7;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      font-size: 14px;
      line-height: 1.5;
    }
    header {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 14px 28px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      font-weight: 700;
      font-size: 16px;
      color: #fff;
      letter-spacing: -0.01em;
    }
    .brand-tag {
      background: rgba(56, 189, 248, 0.12);
      border: 1px solid rgba(56, 189, 248, 0.35);
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 9999px;
      color: var(--accent);
      font-weight: 600;
    }
    .header-actions {
      display: flex;
      gap: 12px;
      align-items: center;
    }
    .status-pill {
      font-size: 12px;
      color: var(--muted);
      background: rgba(255,255,255,0.04);
      padding: 4px 10px;
      border-radius: 6px;
      border: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 6px;
    }
    nav {
      display: flex;
      gap: 4px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 0 28px;
      overflow-x: auto;
    }
    nav button {
      background: none;
      border: none;
      border-bottom: 2px solid transparent;
      color: var(--muted);
      padding: 13px 18px;
      font-weight: 500;
      cursor: pointer;
      font-size: 13px;
      white-space: nowrap;
      transition: color 0.15s, border-color 0.15s;
    }
    nav button:hover {
      color: var(--text);
    }
    nav button.active {
      color: var(--accent);
      border-bottom-color: var(--accent);
      font-weight: 600;
    }
    main {
      max-width: 1440px;
      margin: 0 auto;
      padding: 24px 28px;
    }

    /* Quick Launch Bar */
    .quick-launch-section {
      background: linear-gradient(180deg, rgba(30, 41, 59, 0.6) 0%, rgba(18, 24, 32, 0.8) 100%);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 18px 22px;
      margin-bottom: 24px;
    }
    .quick-launch-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
    }
    .quick-launch-title {
      font-size: 14px;
      font-weight: 600;
      color: #fff;
      display: flex;
      align-items: center;
      gap: 8px;
    }
        .topology-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 16px;
      margin-bottom: 8px;
    }
    .tier-group {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .tier-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid rgba(255,255,255,0.06);
      padding-bottom: 8px;
    }
    .tier-title {
      font-size: 13px;
      font-weight: 700;
      color: #fff;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .tier-badge {
      font-size: 10px;
      padding: 2px 8px;
      border-radius: 9999px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .tier-badge-core { background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }
    .tier-badge-task { background: rgba(168, 85, 247, 0.15); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.3); }
    .tier-badge-app { background: rgba(34, 197, 94, 0.15); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.3); }
    .tier-badge-sensor { background: rgba(234, 179, 8, 0.15); color: #fde047; border: 1px solid rgba(234, 179, 8, 0.3); }

    .comp-item {
      background: rgba(0,0,0,0.25);
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 6px;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      transition: border-color 0.15s;
    }
    .comp-item:hover {
      border-color: rgba(56, 189, 248, 0.4);
    }
    .comp-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .comp-name {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-weight: 700;
      font-size: 13px;
      color: #fff;
    }
    .comp-desc {
      font-size: 12px;
      color: var(--muted);
      line-height: 1.4;
    }
    .comp-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      font-size: 11px;
      color: var(--muted);
    }
    .comp-tag {
      background: rgba(255,255,255,0.04);
      padding: 2px 6px;
      border-radius: 4px;
      border: 1px solid rgba(255,255,255,0.06);
    }
    .comp-actions {
      display: flex;
      gap: 6px;
      align-items: center;
      margin-top: 4px;
      padding-top: 6px;
      border-top: 1px solid rgba(255,255,255,0.04);
    }

    .quick-cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 14px;
    }
    .quick-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 12px;
      transition: transform 0.15s, border-color 0.15s, box-shadow 0.15s;
    }
    .quick-card:hover {
      border-color: rgba(56, 189, 248, 0.4);
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.25);
    }
    .qc-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
    }
    .qc-name {
      font-weight: 600;
      font-size: 14px;
      color: #fff;
    }
    .qc-desc {
      font-size: 12px;
      color: var(--muted);
      margin-top: 2px;
    }
    .qc-route {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 11px;
      color: #7dd3fc;
      background: rgba(125, 211, 252, 0.08);
      padding: 3px 6px;
      border-radius: 4px;
      margin-top: 4px;
      display: inline-block;
    }
    .qc-actions {
      display: flex;
      gap: 8px;
      align-items: center;
      padding-top: 6px;
      border-top: 1px solid rgba(255,255,255,0.05);
    }

    .pulse-dot {
      display: inline-block;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      margin-right: 6px;
    }
    .pulse-running {
      background: var(--success);
      box-shadow: 0 0 8px var(--success);
      animation: pulse 2s infinite;
    }
    .pulse-stopped { background: var(--muted); }
    .pulse-backoff { background: var(--warning); }
    .pulse-failed { background: var(--danger); }
    @keyframes pulse {
      0% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.6; transform: scale(1.15); }
      100% { opacity: 1; transform: scale(1); }
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }
    .metric-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px 20px;
    }
    .metric-title {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 6px;
      font-weight: 600;
    }
    .metric-value {
      font-size: 24px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .section-box {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 22px;
      margin-bottom: 24px;
    }
    h2 {
      font-size: 15px;
      font-weight: 600;
      margin: 0 0 16px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      color: #fff;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      padding: 11px 14px;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }
    th {
      color: var(--muted);
      font-weight: 600;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.03em;
      background: rgba(255,255,255,0.02);
    }
    tr:hover td {
      background: rgba(255, 255, 255, 0.02);
    }
    .badge {
      display: inline-flex;
      align-items: center;
      padding: 2px 9px;
      border-radius: 9999px;
      font-size: 11px;
      font-weight: 600;
      line-height: 1.4;
    }
    .badge-managed { background: rgba(56, 189, 248, 0.14); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }
    .badge-unmanaged { background: rgba(234, 179, 8, 0.14); color: #eab308; border: 1px solid rgba(234, 179, 8, 0.3); }
    .badge-unknown { background: rgba(239, 68, 68, 0.14); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.3); }
    .badge-present, .badge-ready, .badge-running { background: rgba(34, 197, 94, 0.14); color: #22c55e; border: 1px solid rgba(34, 197, 94, 0.3); }
    .badge-stopped { background: rgba(148, 163, 184, 0.1); color: var(--muted); border: 1px solid rgba(148, 163, 184, 0.2); }
    .badge-backoff { background: rgba(234, 179, 8, 0.14); color: #eab308; border: 1px solid rgba(234, 179, 8, 0.3); }
    .badge-failed { background: rgba(239, 68, 68, 0.14); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.3); }

    button.btn, a.btn {
      background: #16a34a;
      color: #fff;
      border: 1px solid rgba(255,255,255,0.15);
      border-radius: 6px;
      padding: 6px 14px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      text-decoration: none;
      transition: background 0.15s, opacity 0.15s;
    }
    button.btn:hover, a.btn:hover { background: #15803d; }
    button.btn-sec, a.btn-sec {
      background: var(--card);
      color: var(--text);
      border: 1px solid var(--border);
    }
    button.btn-sec:hover, a.btn-sec:hover { background: var(--card-hover); color: #fff; }
    button.btn-danger {
      background: #dc2626;
    }
    button.btn-danger:hover { background: #b91c1c; }
    button.btn-open, a.btn-open {
      background: #0284c7;
      color: #fff;
      border: 1px solid rgba(56, 189, 248, 0.4);
    }
    button.btn-open:hover, a.btn-open:hover { background: #0369a1; }
    a.btn-disabled {
      opacity: 0.4;
      pointer-events: none;
      background: var(--card);
      color: var(--muted);
      border-color: var(--border);
    }

    .log-terminal {
      background: #05080c;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      color: #e2e8f0;
      max-height: 540px;
      overflow-y: auto;
      white-space: pre-wrap;
      box-shadow: inset 0 2px 8px rgba(0,0,0,0.5);
    }
    .tab-pane { display: none; }
    .tab-pane.active { display: block; }
    .form-inline {
      display: flex;
      gap: 10px;
      align-items: center;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }
    input, select {
      background: var(--card);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 7px 12px;
      border-radius: 6px;
      font-size: 13px;
      outline: none;
      transition: border-color 0.15s;
    }
    input:focus, select:focus {
      border-color: var(--accent);
    }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: #7dd3fc; }
    a.live-link {
      color: var(--accent);
      text-decoration: none;
      font-weight: 600;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    a.live-link:hover { text-decoration: underline; color: #7dd3fc; }

    /* Toasts */
    .toast-container {
      position: fixed;
      bottom: 24px;
      right: 24px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      z-index: 1000;
    }
    .toast {
      background: var(--surface);
      border: 1px solid var(--border);
      color: #fff;
      padding: 12px 18px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 500;
      box-shadow: 0 10px 25px rgba(0,0,0,0.5);
      display: flex;
      align-items: center;
      gap: 10px;
      animation: slideIn 0.2s ease-out;
    }
    .toast-success { border-left: 4px solid var(--success); }
    .toast-error { border-left: 4px solid var(--danger); }
    .toast-info { border-left: 4px solid var(--accent); }
    @keyframes slideIn {
      from { transform: translateX(100%); opacity: 0; }
      to { transform: translateX(0); opacity: 1; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span>Piceli Operator</span>
      <span class="brand-tag">Piceli Observe R07</span>
    </div>
    <div class="header-actions">
      <div class="status-pill" data-testid="badge-auth" style="border-color: rgba(34, 197, 94, 0.4); background: rgba(34, 197, 94, 0.08); color: #86efac;">
        <span>🔑 <strong>Kabuki:</strong> ihadmin / admin</span>
      </div>
      <div class="status-pill" id="hdr-ns" data-testid="badge-namespace">Namespace: loading...</div>
      <button class="btn btn-sec" id="btn-toggle-refresh" data-testid="btn-autorefresh-toggle" onclick="toggleAutoRefresh()">Auto-Refresh: ON (3s)</button>
      <button class="btn btn-sec" data-testid="btn-refresh" onclick="loadAll()">↻ Refresh</button>
    </div>
  </header>

  <nav>
    <button class="active" data-testid="nav-inventory" onclick="switchTab('inventory', this)">Overview & Inventory</button>
    <button data-testid="nav-forwards" onclick="switchTab('forwards', this)">Port Forwards & Shortcuts</button>
    <button data-testid="nav-releases" onclick="switchTab('releases', this)">Releases & History</button>
    <button data-testid="nav-automation" onclick="switchTab('automation', this)">Git & PR Automation</button>
    <button data-testid="nav-artifacts" onclick="switchTab('artifacts', this)">Artifacts & Safe GC</button>
    <button data-testid="nav-logs" onclick="switchTab('logs', this)">Workload Logs</button>
    <button data-testid="nav-state" onclick="switchTab('state', this)">State & Backup</button>
  </nav>

  <main>
    <!-- Top Quick Launch Bar -->
    <section class="quick-launch-section" data-testid="quick-launch-bar">
      <div class="quick-launch-header">
        <div class="quick-launch-title">
          <span>⚡ One-Click Forwarding Shortcuts</span>
          <span style="font-size: 11px; font-weight: normal; color: var(--muted);">Click Start to bridge network port directly to 127.0.0.1</span>
        </div>
      </div>
      <div class="quick-cards" id="quick-cards-container">
        <!-- Rendered by JavaScript -->
        <div style="color: var(--muted); font-size: 12px;">Loading shortcuts...</div>
      </div>
    </section>

    <!-- TAB 1: INVENTORY -->
    <div id="tab-inventory" class="tab-pane active">
      <div class="metrics">
        <div class="metric-card" data-testid="metric-managed">
          <div class="metric-title">Managed Workloads</div>
          <div class="metric-value" id="m-managed" style="color: var(--accent);">0</div>
        </div>
        <div class="metric-card" data-testid="metric-unmanaged">
          <div class="metric-title">Unmanaged Visible</div>
          <div class="metric-value" id="m-unmanaged" style="color: var(--warning);">0</div>
        </div>
        <div class="metric-card" data-testid="metric-unknown">
          <div class="metric-title">Unknown / Failed</div>
          <div class="metric-value" id="m-unknown" style="color: var(--danger);">0</div>
        </div>
        <div class="metric-card" data-testid="metric-release">
          <div class="metric-title">Active Release</div>
          <div class="metric-value" id="m-release" style="font-size: 16px; color: var(--success);">None</div>
        </div>
      </div>

      <!-- System Topology & Architecture Tiers View -->
      <div class="section-box" data-testid="section-system-topology">
        <h2>
          <span>🏛️ System Topology & Component Architecture</span>
          <span style="font-size: 11px; font-weight: normal; color: var(--muted);">Infinite Haiku Canonical Event-Driven & Telemetry Architecture</span>
        </h2>
        <div class="topology-grid" id="topology-container" data-testid="topology-grid">
          <div style="color: var(--muted); font-size: 12px;">Loading topology...</div>
        </div>
      </div>

      <div class="section-box">
        <h2>Managed Resources (Piceli Declared)</h2>
        <table>
          <thead>
            <tr><th>Kind</th><th>Name</th><th>State</th><th>Phase</th><th>Images</th></tr>
          </thead>
          <tbody id="tbl-managed" data-testid="tbl-managed"><tr><td colspan="5" style="color: var(--muted);">Loading...</td></tr></tbody>
        </table>
      </div>

      <div class="section-box">
        <h2>Unmanaged Visible Objects</h2>
        <table>
          <thead>
            <tr><th>Kind</th><th>Namespace / Name</th><th>Phase</th></tr>
          </thead>
          <tbody id="tbl-unmanaged" data-testid="tbl-unmanaged"><tr><td colspan="3" style="color: var(--muted);">None observed</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- TAB 2: PORT FORWARDS & SHORTCUTS -->
    <div id="tab-forwards" class="tab-pane">
      <div class="section-box">
        <h2>Active & Supervised Loopback Forwards</h2>
        <table>
          <thead>
            <tr><th>Name</th><th>Namespace</th><th>Target</th><th>Local Port</th><th>Remote Port</th><th>Live Web Link</th><th>Status</th><th>Actions</th></tr>
          </thead>
          <tbody id="tbl-forwards" data-testid="tbl-forwards"><tr><td colspan="8" style="color: var(--muted);">No forwards active</td></tr></tbody>
        </table>
      </div>

      <div class="section-box">
        <h2>Add Custom Port Forward</h2>
        <form class="form-inline" id="form-add-forward" data-testid="form-add-forward" onsubmit="event.preventDefault(); addCustomForward();">
          <input type="text" id="add-fwd-name" data-testid="input-fwd-name" placeholder="Name (e.g. debug-api)" required style="width: 140px;">
          <input type="text" id="add-fwd-target" data-testid="input-fwd-target" placeholder="Target (service/name or pod/name)" required style="width: 220px;">
          <input type="number" id="add-fwd-local" data-testid="input-fwd-local-port" placeholder="Local Port" required style="width: 110px;">
          <input type="number" id="add-fwd-remote" data-testid="input-fwd-remote-port" placeholder="Remote Port" required style="width: 110px;">
          <input type="text" id="add-fwd-ns" data-testid="input-fwd-namespace" placeholder="Namespace" style="width: 140px;">
          <label style="font-size: 12px; display: flex; align-items: center; gap: 4px;">
            <input type="checkbox" id="add-fwd-start" data-testid="chk-fwd-start" checked> Start Now
          </label>
          <button type="submit" class="btn" data-testid="btn-add-forward">+ Add & Start</button>
        </form>
      </div>
    </div>

    <!-- TAB 3: RELEASES -->
    <div id="tab-releases" class="tab-pane">
      <div class="section-box">
        <h2>Release Catalog & Provenance Lineage</h2>
        <div class="form-inline">
          <input type="text" id="promo-source" data-testid="input-promo-source" placeholder="Source Release (e.g. branch-a)">
          <input type="text" id="promo-target" data-testid="input-promo-target" placeholder="Target Tag (e.g. production)">
          <button class="btn" data-testid="btn-promote-release" onclick="promoteRelease()">Promote Digest (No Rebuild)</button>
        </div>
        <table>
          <thead>
            <tr><th>Name</th><th>Source Kind</th><th>Identity</th><th>Artifact Digest</th><th>Status</th><th>Actions</th></tr>
          </thead>
          <tbody id="tbl-releases" data-testid="tbl-releases"></tbody>
        </table>
      </div>
    </div>

    <!-- TAB 4: AUTOMATION -->
    <div id="tab-automation" class="tab-pane">
      <div class="section-box">
        <h2>Git Branch & PR Watcher (Opt-In Automation)</h2>
        <p style="color: var(--muted); font-size: 12px;">Security Invariant: Untrusted PR code NEVER runs with deployment credentials. Deploying requires explicit operator approval.</p>
        <table>
          <thead>
            <tr><th>Ref</th><th>Commit</th><th>Type</th><th>Trusted</th><th>Status</th></tr>
          </thead>
          <tbody id="tbl-branches" data-testid="tbl-branches"><tr><td colspan="5" style="color: var(--muted);">No watched branches configured</td></tr></tbody>
        </table>
      </div>
      <div class="section-box">
        <h2>Standing Policies</h2>
        <table>
          <thead>
            <tr><th>Policy</th><th>Allowed Namespaces</th><th>Allowed Operations</th><th>PR Approval</th></tr>
          </thead>
          <tbody id="tbl-policies" data-testid="tbl-policies"></tbody>
        </table>
      </div>
    </div>

    <!-- TAB 5: ARTIFACTS & SAFE GC -->
    <div id="tab-artifacts" class="tab-pane">
      <div class="metrics">
        <div class="metric-card" data-testid="metric-art-total">
          <div class="metric-title">Total Image Space</div>
          <div class="metric-value" id="art-total">0 B</div>
        </div>
        <div class="metric-card" data-testid="metric-art-prot">
          <div class="metric-title">Protected Space</div>
          <div class="metric-value" id="art-prot" style="color: var(--success);">0 B</div>
        </div>
        <div class="metric-card" data-testid="metric-art-reclaim">
          <div class="metric-title">Reclaimable Space</div>
          <div class="metric-value" id="art-reclaim" style="color: var(--warning);">0 B</div>
        </div>
      </div>
      <div class="section-box">
        <h2>Cluster Image Space & Safe GC</h2>
        <div class="form-inline">
          <label><input type="checkbox" id="gc-dryrun" data-testid="chk-gc-dryrun" checked> Dry Run</label>
          <button class="btn btn-danger" data-testid="btn-run-gc" onclick="runGC()">Run Safe GC</button>
        </div>
        <div id="gc-receipt" style="margin-bottom: 12px;"></div>
        <table>
          <thead>
            <tr><th>Digest</th><th>Platforms</th><th>Nodes</th><th>Refs (Running / Release)</th><th>Protected</th></tr>
          </thead>
          <tbody id="tbl-artifacts" data-testid="tbl-artifacts"></tbody>
        </table>
      </div>
    </div>

    <!-- TAB 6: LOGS -->
    <div id="tab-logs" class="tab-pane">
      <div class="section-box">
        <h2>Bounded Workload Logs</h2>
        <div class="form-inline">
          <input type="text" id="log-target" data-testid="input-log-target" placeholder="Target (e.g. deployment/app or pod/...)" style="width: 280px;">
          <input type="number" id="log-tail" data-testid="input-log-tail" value="200" min="1" max="1000" style="width: 90px;">
          <input type="text" id="log-container" data-testid="input-log-container" placeholder="Container (optional)">
          <label><input type="checkbox" id="log-prev" data-testid="chk-log-prev"> Previous</label>
          <button class="btn" data-testid="btn-fetch-logs" onclick="fetchLogs()">Fetch Logs</button>
        </div>
        <div class="log-terminal" id="log-output" data-testid="log-output">Logs will appear here...</div>
      </div>
    </div>

    <!-- TAB 7: STATE & BACKUP -->
    <div id="tab-state" class="tab-pane">
      <div class="section-box">
        <h2>State Store & Single-Instance Safety</h2>
        <p style="color: var(--muted);">State is persisted atomically to volume storage with <code>0o600</code> permissions and <code>fcntl.flock</code> locking.</p>
        <button class="btn" data-testid="btn-create-backup" onclick="createBackup()">Create Backup Archive (.tar.gz)</button>
        <div id="backup-status" style="margin-top: 14px;"></div>
      </div>
    </div>
  </main>

  <div id="toasts" class="toast-container"></div>

  <script>
    const COMPONENT_TIERS = [
      {
        id: "core",
        title: "Core Telemetry & Datastore Engine",
        badge: "Storage & Ingestion",
        badgeClass: "tier-badge-core",
        components: [
          {
            name: "ih-target-poet",
            role: "Target Poet Ingestion & Store",
            desc: "Native telemetry datastore, OTLP ingestion, and temporal multi-view graph engine.",
            ports: "18080 (HTTP) / 18443 (TLS)",
            shortcutId: "poet",
            url: "http://127.0.0.1:18086"
          },
          {
            name: "ih-observer-poet",
            role: "Observer Poet Analytics",
            desc: "Read-only replica for historical analytics and long-term telemetry retention.",
            ports: "18081",
            shortcutId: null,
            url: null
          }
        ]
      },
      {
        id: "task",
        title: "Distributed Task & Coordination Tier",
        badge: "Shibuya & Rustvello",
        badgeClass: "tier-badge-task",
        components: [
          {
            name: "ih-redis",
            role: "State & Queue Store",
            desc: "Redis 7 persistent backing store for Shibuya domain state and Rustvello task distribution.",
            ports: "6379 (Plain) / 6380 (TLS)",
            shortcutId: null,
            url: null
          },
          {
            name: "ih-shibuya",
            role: "Domain Task Coordinator",
            desc: "Domain orchestrator and event hub for offloading compute tasks to Rustvello.",
            ports: "18083",
            shortcutId: "shibuya",
            url: "http://127.0.0.1:18083"
          },
          {
            name: "ih-worker",
            role: "Rustvello Task Worker",
            desc: "High-performance worker pulling and executing offloaded generation tasks.",
            ports: "Internal Worker",
            shortcutId: null,
            url: null
          }
        ]
      },
      {
        id: "app",
        title: "Application & Presentation Tier",
        badge: "Kabuki & Monitor",
        badgeClass: "tier-badge-app",
        components: [
          {
            name: "ih-kabuki",
            role: "Leptos SSR Web UI & Studio",
            desc: "Browser user interface, pilot bridge, and interactive session manager.",
            ports: "3000",
            shortcutId: "kabuki",
            url: "http://127.0.0.1:3000/login"
          },
          {
            name: "ih-rustvello-monitor",
            role: "Task Telemetry Dashboard",
            desc: "Realtime observability interface for task worker queues and execution progress.",
            ports: "18084",
            shortcutId: "monitor",
            url: "http://127.0.0.1:18084"
          }
        ]
      },
      {
        id: "sensors",
        title: "Autonomous Sensors & Muses",
        badge: "Telemetry Harvester",
        badgeClass: "tier-badge-sensor",
        components: [
          {
            name: "ih-observer-muse",
            role: "Cross-Node Telemetry Sensor",
            desc: "Autonomous agent harvesting host and service metrics between target and observer.",
            ports: "Autonomous Sensor",
            shortcutId: null,
            url: null
          }
        ]
      }
    ];

    function viewLogsFor(target) {
      const input = document.getElementById('log-target');
      if (input) input.value = target;
      switchTab('logs', document.querySelector('[data-testid="nav-logs"]'));
      fetchLogs();
    }

    const token = __PICELI_TOKEN__;
    let autoRefreshActive = true;
    let autoRefreshTimer = null;
    let currentNamespace = '';

    const esc = v => String(v ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

    function showToast(msg, type = 'info') {
      const c = document.getElementById('toasts');
      const el = document.createElement('div');
      el.className = `toast toast-${type}`;
      el.textContent = msg;
      c.appendChild(el);
      setTimeout(() => {
        el.style.opacity = '0';
        el.style.transition = 'opacity 0.3s';
        setTimeout(() => el.remove(), 300);
      }, 3500);
    }

    function switchTab(name, btn) {
      document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('nav button').forEach(el => el.classList.remove('active'));
      const target = document.getElementById('tab-' + name);
      if (target) target.classList.add('active');
      if (btn) btn.classList.add('active');
    }

    function toggleAutoRefresh() {
      autoRefreshActive = !autoRefreshActive;
      const btn = document.getElementById('btn-toggle-refresh');
      if (autoRefreshActive) {
        btn.textContent = 'Auto-Refresh: ON (3s)';
        btn.classList.remove('btn-danger');
        btn.classList.add('btn-sec');
        startRefreshLoop();
        showToast('Auto-refresh resumed', 'info');
      } else {
        btn.textContent = 'Auto-Refresh: PAUSED';
        btn.classList.remove('btn-sec');
        btn.classList.add('btn-danger');
        if (autoRefreshTimer) clearInterval(autoRefreshTimer);
        showToast('Auto-refresh paused', 'info');
      }
    }

    async function req(url, method = 'GET', body = null) {
      const opts = {
        method,
        headers: {
          'Content-Type': 'application/json',
          'X-Piceli-Local-Token': token
        }
      };
      if (body) opts.body = JSON.stringify(body);
      const res = await fetch(url, opts);
      return res.json();
    }

    async function loadAll() {
      try {
        const [status, releases, forwards, artifacts, auto, shortcutsData] = await Promise.all([
          req('/v1/status'),
          req('/v1/releases'),
          req('/v1/forwards'),
          req('/v1/artifacts'),
          req('/v1/automation'),
          req('/v1/shortcuts'),
        ]);

        currentNamespace = status.namespace || 'infinite-haiku-p2';
        document.getElementById('hdr-ns').textContent = 'Namespace: ' + currentNamespace;
        const addNsInput = document.getElementById('add-fwd-ns');
        if (addNsInput && !addNsInput.value) addNsInput.value = currentNamespace;

        document.getElementById('m-managed').textContent = (status.managed || []).length;
        document.getElementById('m-unmanaged').textContent = (status.unmanaged || []).length;
        document.getElementById('m-unknown').textContent = (status.unknown || []).length;
        document.getElementById('m-release').textContent = status.active_release || 'None';

        // Render Quick Shortcuts Cards
        const shortcuts = shortcutsData.shortcuts || [];
        const shortcutsHtml = shortcuts.map(sc => {
          const isRunning = (sc.state === 'running');
          const isBackoff = (sc.state === 'backoff');
          const isFailed = (sc.state === 'failed');
          const pulseClass = isRunning ? 'pulse-running' : (isBackoff ? 'pulse-backoff' : (isFailed ? 'pulse-failed' : 'pulse-stopped'));
          const stateLabel = isRunning ? `Running (PID ${sc.pid})` : (sc.state || 'stopped');

          return `
            <div class="quick-card" data-testid="shortcut-card-${esc(sc.id)}">
              <div class="qc-top">
                <div>
                  <div class="qc-name">${esc(sc.label)}</div>
                  <div class="qc-desc">${esc(sc.description)}</div>
                  <div class="qc-route">${esc(sc.target)} → 127.0.0.1:${esc(sc.local_port)}</div>
                </div>
                <span class="badge badge-${esc(sc.state)}">
                  <span class="pulse-dot ${pulseClass}"></span>${esc(stateLabel)}
                </span>
              </div>
              <div class="qc-actions">
                ${isRunning ? `
                  <button class="btn btn-danger" data-testid="btn-stop-${esc(sc.id)}" onclick="actQuickShortcut('${esc(sc.id)}', 'stop')">Stop</button>
                  <a href="${esc(sc.url)}" target="_blank" data-testid="link-open-${esc(sc.id)}" class="btn btn-open">Open ↗</a>
                ` : `
                  <button class="btn" data-testid="btn-start-${esc(sc.id)}" onclick="actQuickShortcut('${esc(sc.id)}', 'start')">▶ Start Forward</button>
                  <a href="${esc(sc.url)}" target="_blank" data-testid="link-open-${esc(sc.id)}" class="btn btn-disabled">Open ↗</a>
                `}
              </div>
            </div>
          `;
        }).join('');
        document.getElementById('quick-cards-container').innerHTML = shortcutsHtml || '<div style="color: var(--muted);">No shortcuts configured</div>';

        // Render System Topology Cards
        const managedMap = new Map();
        (status.managed || []).forEach(m => managedMap.set(m.ref.name, m));

        const topologyHtml = COMPONENT_TIERS.map(tier => `
          <div class="tier-group" data-testid="tier-group-${esc(tier.id)}">
            <div class="tier-header">
              <div class="tier-title">${esc(tier.title)}</div>
              <span class="tier-badge ${esc(tier.badgeClass)}">${esc(tier.badge)}</span>
            </div>
            ${tier.components.map(comp => {
              const res = managedMap.get(comp.name);
              const isPresent = res && res.state === 'present';
              const phase = res?.observed?.phase || (isPresent ? 'Running' : 'Not Deployed');
              const images = (res?.observed?.images || []).join(', ') || 'canonical';
              const shortcut = shortcuts.find(s => s.id === comp.shortcutId);
              const fwdRunning = shortcut && shortcut.state === 'running';

              return `
                <div class="comp-item" data-testid="topology-card-${esc(comp.name)}">
                  <div class="comp-header">
                    <span class="comp-name">${esc(comp.name)}</span>
                    <span class="badge ${isPresent ? 'badge-running' : 'badge-stopped'}">
                      <span class="pulse-dot ${isPresent ? 'pulse-running' : 'pulse-stopped'}"></span>${esc(phase)}
                    </span>
                  </div>
                  <div class="comp-desc">${esc(comp.desc)}</div>
                  <div class="comp-meta">
                    <span class="comp-tag">Role: <strong>${esc(comp.role)}</strong></span>
                    <span class="comp-tag">Port: <code>${esc(comp.ports)}</code></span>
                  </div>
                  <div class="comp-actions">
                    ${comp.shortcutId ? (fwdRunning ? `
                      <button class="btn btn-danger" style="padding: 3px 8px; font-size: 11px;" onclick="actQuickShortcut('${esc(comp.shortcutId)}', 'stop')">■ Stop</button>
                      <a href="${esc(comp.url)}" target="_blank" class="btn btn-open" style="padding: 3px 8px; font-size: 11px;">Open ↗</a>
                    ` : `
                      <button class="btn" style="padding: 3px 8px; font-size: 11px;" onclick="actQuickShortcut('${esc(comp.shortcutId)}', 'start')">▶ Forward</button>
                      <a href="${esc(comp.url)}" target="_blank" class="btn btn-disabled" style="padding: 3px 8px; font-size: 11px;">Open ↗</a>
                    `) : ''}
                    <button class="btn btn-sec" style="padding: 3px 8px; font-size: 11px;" onclick="viewLogsFor('deployment/${esc(comp.name)}')">Logs ↗</button>
                  </div>
                </div>
              `;
            }).join('')}
          </div>
        `).join('');
        document.getElementById('topology-container').innerHTML = topologyHtml;

        // Managed table
        document.getElementById('tbl-managed').innerHTML = (status.managed || []).map(r => `
          <tr data-testid="row-managed-${esc(r.ref.name)}">
            <td>${esc(r.ref.kind)}</td>
            <td><code>${esc(r.ref.name)}</code></td>
            <td><span class="badge badge-${esc(r.state)}">${esc(r.state)}</span></td>
            <td>${esc(r.observed?.phase || '-')}</td>
            <td><code>${esc((r.observed?.images || []).join(', ') || '-')}</code></td>
          </tr>
        `).join('') || '<tr><td colspan="5" style="color:var(--muted);">No declared resources</td></tr>';

        // Unmanaged table
        document.getElementById('tbl-unmanaged').innerHTML = (status.unmanaged || []).map(r => `
          <tr>
            <td>${esc(r.ref.kind)}</td>
            <td><code>${esc(r.ref.namespace + '/' + r.ref.name)}</code></td>
            <td>${esc(r.observed?.phase || '-')}</td>
          </tr>
        `).join('') || '<tr><td colspan="3" style="color:var(--muted);">No unmanaged objects found</td></tr>';

        // Releases table
        document.getElementById('tbl-releases').innerHTML = (releases.releases || []).map(rel => `
          <tr>
            <td><strong>${esc(rel.name)}</strong></td>
            <td>${esc(rel.kind || rel.source_type || '-')}</td>
            <td><code>${esc(rel.identity || '-')}</code></td>
            <td><code>${esc(rel.artifact_digest ? rel.artifact_digest.slice(0, 24) + '...' : '-')}</code></td>
            <td>${rel.is_active ? '<span class="badge badge-present">Active</span>' : '<span class="badge badge-stopped">Historical</span>'}</td>
            <td>
              <button class="btn btn-sec" onclick="rollbackRelease('${esc(rel.name)}')">Rollback</button>
            </td>
          </tr>
        `).join('') || '<tr><td colspan="6" style="color:var(--muted);">No releases catalogued</td></tr>';

        // Forwards table
        document.getElementById('tbl-forwards').innerHTML = (forwards.forwards || []).map(f => {
          const isRunning = (f.state === 'running');
          const liveUrl = `http://127.0.0.1:${f.local_port}`;
          return `
            <tr data-testid="row-fwd-${esc(f.name)}">
              <td><strong>${esc(f.name)}</strong></td>
              <td><code>${esc(f.namespace || currentNamespace)}</code></td>
              <td><code>${esc(f.target || '-')}</code></td>
              <td><code>${esc(f.local_port || '-')}</code></td>
              <td><code>${esc(f.remote_port || '-')}</code></td>
              <td>
                ${isRunning ? `<a href="${esc(liveUrl)}" target="_blank" class="live-link" data-testid="link-fwd-${esc(f.name)}">${esc(liveUrl)} ↗</a>` : '<span style="color:var(--muted);">-</span>'}
              </td>
              <td><span class="badge badge-${esc(f.state)}">${esc(f.state)}${f.pid ? ' (' + f.pid + ')' : ''}</span></td>
              <td>
                ${isRunning ? `
                  <button class="btn btn-danger" data-testid="btn-stop-fwd-${esc(f.name)}" onclick="actForward('${esc(f.name)}','stop')">Stop</button>
                ` : `
                  <button class="btn" data-testid="btn-start-fwd-${esc(f.name)}" onclick="actForward('${esc(f.name)}','start')">Start</button>
                `}
                <button class="btn btn-sec" data-testid="btn-del-fwd-${esc(f.name)}" onclick="deleteForward('${esc(f.name)}')">✕</button>
              </td>
            </tr>
          `;
        }).join('') || '<tr><td colspan="8" style="color:var(--muted);">No forwards supervised. Use shortcuts above or add one below.</td></tr>';

        // Artifacts
        if (artifacts.total_bytes !== undefined) {
          document.getElementById('art-total').textContent = (artifacts.total_bytes / 1e6).toFixed(1) + ' MB';
          document.getElementById('art-prot').textContent = (artifacts.protected_bytes / 1e6).toFixed(1) + ' MB';
          document.getElementById('art-reclaim').textContent = (artifacts.reclaimable_bytes / 1e6).toFixed(1) + ' MB';
          document.getElementById('tbl-artifacts').innerHTML = (artifacts.entries || []).map(e => `
            <tr>
              <td><code>${esc(e.digest.slice(0, 24))}...</code></td>
              <td>${esc((e.platforms || []).join(', '))}</td>
              <td>${esc((e.nodes || []).join(', ') || 'local')}</td>
              <td>Running: ${esc((e.running_refs || []).length)} | Release: ${esc((e.release_refs || []).length)}</td>
              <td>${e.is_protected ? '<span class="badge badge-present">Protected</span>' : '<span class="badge badge-unmanaged">Reclaimable</span>'}</td>
            </tr>
          `).join('');
        }

        // Policies
        document.getElementById('tbl-policies').innerHTML = (auto.policies || []).map(p => `
          <tr>
            <td><strong>${esc(p.name)}</strong></td>
            <td>${esc(p.allowed_namespaces.join(', '))}</td>
            <td>${esc(p.allowed_operations.join(', '))}</td>
            <td>${p.require_pr_approval ? 'Required' : 'Opt-In'}</td>
          </tr>
        `).join('') || '<tr><td colspan="4" style="color:var(--muted);">No standing policies</td></tr>';

      } catch (err) {
        console.error('Failed to load operator state:', err);
      }
    }

    async function actQuickShortcut(id, action) {
      showToast(`${action === 'start' ? 'Starting' : 'Stopping'} ${id}...`, 'info');
      const res = await req('/v1/forwards/quick', 'POST', { shortcut: id, action: action, namespace: currentNamespace });
      if (res.error) {
        showToast('Error: ' + res.error, 'error');
      } else {
        showToast(`${id} ${action === 'start' ? 'started' : 'stopped'} successfully!`, 'success');
      }
      loadAll();
    }

    async function actForward(name, action) {
      showToast(`${action === 'start' ? 'Starting' : 'Stopping'} forward ${name}...`, 'info');
      const res = await req('/v1/forwards/' + action, 'POST', { name });
      if (res.error) {
        showToast('Error: ' + res.error, 'error');
      } else {
        showToast(`Forward ${name} ${action === 'start' ? 'started' : 'stopped'}`, 'success');
      }
      loadAll();
    }

    async function deleteForward(name) {
      if (!confirm(`Delete saved forward ${name}?`)) return;
      const res = await req('/v1/forwards/delete', 'POST', { name });
      if (res.error) {
        showToast('Error: ' + res.error, 'error');
      } else {
        showToast(`Deleted forward ${name}`, 'info');
      }
      loadAll();
    }

    async function addCustomForward() {
      const name = document.getElementById('add-fwd-name').value.trim();
      const target = document.getElementById('add-fwd-target').value.trim();
      const local = parseInt(document.getElementById('add-fwd-local').value || '0', 10);
      const remote = parseInt(document.getElementById('add-fwd-remote').value || '0', 10);
      const ns = document.getElementById('add-fwd-ns').value.trim() || currentNamespace;
      const startNow = document.getElementById('add-fwd-start').checked;

      if (!name || !target || !local || !remote) {
        return showToast('All forward fields required', 'error');
      }

      showToast(`Adding port forward ${name}...`, 'info');
      const res = await req('/v1/forwards/add', 'POST', {
        name, target, local_port: local, remote_port: remote, namespace: ns, start: startNow
      });
      if (res.error) {
        showToast('Error adding forward: ' + res.error, 'error');
      } else {
        showToast(`Forward ${name} added successfully!`, 'success');
        document.getElementById('add-fwd-name').value = '';
        document.getElementById('add-fwd-target').value = '';
        document.getElementById('add-fwd-local').value = '';
        document.getElementById('add-fwd-remote').value = '';
      }
      loadAll();
    }

    async function promoteRelease() {
      const source = document.getElementById('promo-source').value.trim();
      const target = document.getElementById('promo-target').value.trim();
      if (!source || !target) return showToast('Source and target required', 'error');
      const res = await req('/v1/releases/promote', 'POST', { source_name: source, target_name: target });
      if (res.error) {
        showToast('Error: ' + res.error, 'error');
      } else {
        showToast(`Promoted ${target} successfully without rebuild!`, 'success');
      }
      loadAll();
    }

    async function rollbackRelease(name) {
      if (!confirm('Rollback to release ' + name + '?')) return;
      const res = await req('/v1/releases/rollback', 'POST', { target_release_name: name });
      if (res.error) {
        showToast('Rollback error: ' + res.error, 'error');
      } else {
        showToast('Rollback complete to: ' + name, 'success');
      }
      loadAll();
    }

    async function runGC() {
      const dry = document.getElementById('gc-dryrun').checked;
      showToast('Running safe GC...', 'info');
      const res = await req('/v1/artifacts/gc', 'POST', { dry_run: dry });
      if (res.error) {
        showToast('GC error: ' + res.error, 'error');
      } else {
        document.getElementById('gc-receipt').innerHTML = `
          <div class="badge badge-present">Freed: ${(res.freed_bytes/1e6).toFixed(2)} MB | Pruned: ${res.pruned_digests.length} digests (Dry run: ${res.dry_run})</div>
        `;
        showToast(`GC completed: ${res.pruned_digests.length} images pruned (Dry run: ${res.dry_run})`, 'success');
      }
      loadAll();
    }

    async function fetchLogs() {
      const target = document.getElementById('log-target').value.trim();
      const tail = parseInt(document.getElementById('log-tail').value || '200', 10);
      const container = document.getElementById('log-container').value.trim();
      const prev = document.getElementById('log-prev').checked;
      if (!target) return showToast('Log target required (e.g. deployment/app)', 'error');

      showToast('Fetching logs for ' + target + '...', 'info');
      const res = await req('/v1/logs?target=' + encodeURIComponent(target) + '&tail=' + tail + '&container=' + encodeURIComponent(container) + '&previous=' + prev);
      document.getElementById('log-output').textContent = (res.lines || []).join(String.fromCharCode(10)) || res.error || 'No log lines returned.';
    }

    async function createBackup() {
      showToast('Creating backup archive...', 'info');
      const res = await req('/v1/backup/create', 'POST');
      document.getElementById('backup-status').innerHTML = res.ok ?
        `<span class="badge badge-present">Backup created: ${esc(res.backup_path)}</span>` :
        `<span class="badge badge-unknown">Backup error: ${esc(res.error)}</span>`;
      if (res.ok) showToast('Backup created successfully', 'success');
    }

    function startRefreshLoop() {
      if (autoRefreshTimer) clearInterval(autoRefreshTimer);
      autoRefreshTimer = setInterval(() => {
        if (autoRefreshActive) loadAll();
      }, 3000);
    }

    loadAll();
    startRefreshLoop();
  </script>
</body>
</html>
"""


class LocalObserveServer(ThreadingHTTPServer):
    """A loopback-only server exposing current inventory, releases, artifacts, and preferences."""

    def __init__(
        self,
        address: tuple[str, int],
        report: Callable[[], Any],
        preferences: PreferenceStore,
        supervisor: ForwardSupervisor | None = None,
        user: str | None = None,
        *,
        catalog: ReleaseCatalog | None = None,
        state_store: FileStateStore | None = None,
        workflow: ReleaseWorkflow | None = None,
        artifact_inventory: ImageSpaceInventory | None = None,
        log_reader_fn: Callable[..., list[str]] | None = None,
        namespace: str = "default",
    ) -> None:
        if address[0] not in {"127.0.0.1", "::1"}:
            raise ValueError("Piceli observe server must bind to loopback")
        self.report = report
        self.preferences = preferences
        self.supervisor = supervisor
        self.user = user
        self.catalog = catalog
        self.state_store = state_store
        self.workflow = workflow
        self.artifact_inventory = artifact_inventory
        self.log_reader_fn = log_reader_fn
        self.namespace = namespace
        self.local_token = secrets.token_urlsafe(24)
        super().__init__(address, LocalObserveHandler)


class LocalObserveHandler(BaseHTTPRequestHandler):
    """Serve unified versioned REST API and semantic operator UI with identical authorization."""

    server: LocalObserveServer

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep user preferences, tokens, and secret paths out of terminal logs."""

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self) -> None:
        body = _PAGE_HTML.replace(
            "__PICELI_TOKEN__", json.dumps(self.server.local_token)
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        """Verify token via X-Piceli-Local-Token or Bearer user store."""
        token_hdr = self.headers.get("X-Piceli-Local-Token")
        if token_hdr and secrets.compare_digest(token_hdr, self.server.local_token):
            return True
        auth_hdr = self.headers.get("Authorization")
        if auth_hdr and auth_hdr.startswith("Bearer ") and self.server.state_store:
            token = auth_hdr.split(" ", 1)[1]
            user = UserStore(self.server.state_store).authenticate(token)
            if user:
                return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._html()
            return
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if self.path == "/healthz":
            self._json(200, {"ok": True})
            return
        if self.path == "/v1/status":
            try:
                self._json(200, self.server.report().to_dict())
            except Exception as error:
                self._json(503, {"error": type(error).__name__})
            return
        if self.path == "/v1/preferences":
            users = self.server.preferences.load()
            selected = (
                ((users[self.server.user],) if self.server.user in users else ())
                if self.server.user
                else tuple(users.values())
            )
            self._json(
                200,
                {
                    "users": [
                        {
                            "user": item.user,
                            "forwards": [forward.__dict__ for forward in item.forwards],
                        }
                        for item in sorted(selected, key=lambda item: item.user)
                    ]
                },
            )
            return
        if self.path == "/v1/shortcuts":
            shortcuts = (
                self.server.supervisor.shortcuts_status(self.server.namespace)
                if self.server.supervisor
                else []
            )
            self._json(200, {"shortcuts": shortcuts})
            return
        if self.path == "/v1/forwards":
            statuses = (
                self.server.supervisor.statuses() if self.server.supervisor else ()
            )
            self._json(200, {"forwards": [item.__dict__ for item in statuses]})
            return
        if self.path == "/v1/releases":
            records = []
            selected_name = None
            if self.server.catalog:
                try:
                    selected_name = self.server.catalog.selected().name
                except Exception:
                    pass
                for r in self.server.catalog.records():
                    records.append({
                        "name": r.name,
                        "namespace": r.namespace,
                        "kind": r.source.kind,
                        "identity": r.source.identity,
                        "artifact_digest": r.source.artifact_digest,
                        "is_active": (r.name == selected_name),
                    })
            self._json(200, {"selected": selected_name, "releases": records})
            return
        if self.path == "/v1/artifacts":
            if self.server.artifact_inventory:
                self._json(200, self.server.artifact_inventory.to_dict())
            else:
                self._json(200, {"total_images": 0, "entries": [], "total_bytes": 0, "protected_bytes": 0, "reclaimable_bytes": 0})
            return
        if self.path == "/v1/automation":
            policies_data = []
            if self.server.state_store:
                ps = PolicyStore(self.server.state_store).load_policies()
                for p in ps.values():
                    policies_data.append({
                        "name": p.name,
                        "allowed_namespaces": list(p.allowed_namespaces),
                        "allowed_operations": list(p.allowed_operations),
                        "require_pr_approval": p.require_pr_approval,
                    })
            self._json(200, {"policies": policies_data, "watched_branches": []})
            return
        if self.path.startswith("/v1/logs"):
            if self.server.log_reader_fn:
                try:
                    lines = self.server.log_reader_fn()
                    self._json(200, {"lines": lines})
                except Exception as e:
                    self._json(500, {"error": str(e)})
            else:
                self._json(200, {"lines": ["Log reader adapter not attached."]})
            return

        self._json(404, {"error": "not-found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_auth():
            self._json(403, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length)) if length > 0 else {}
        except Exception:
            self._json(400, {"error": "invalid-json-payload"})
            return

        if self.path in {"/v1/forwards/start", "/v1/forwards/stop"}:
            if self.server.supervisor is None:
                self._json(409, {"error": "forward-supervision-disabled"})
                return
            name = payload.get("name")
            if not isinstance(name, str):
                self._json(400, {"error": "invalid-forward-name"})
                return
            try:
                if self.path.endswith("/start"):
                    self.server.supervisor.start(name)
                else:
                    self.server.supervisor.stop(name)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if self.path == "/v1/forwards/quick":
            if self.server.supervisor is None:
                self._json(409, {"error": "forward-supervision-disabled"})
                return
            shortcut = payload.get("shortcut")
            action = payload.get("action", "start")
            ns = payload.get("namespace", self.server.namespace)
            if not isinstance(shortcut, str):
                self._json(400, {"error": "invalid-shortcut"})
                return
            try:
                if action == "stop":
                    self.server.supervisor.stop(shortcut)
                else:
                    self.server.supervisor.quick_start(shortcut, namespace=ns)
                shortcuts = self.server.supervisor.shortcuts_status(self.server.namespace)
                self._json(200, {"ok": True, "shortcut": shortcut, "shortcuts": shortcuts})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if self.path == "/v1/forwards/add":
            if self.server.supervisor is None:
                self._json(409, {"error": "forward-supervision-disabled"})
                return
            try:
                name = payload.get("name")
                target = payload.get("target")
                local_port = int(payload.get("local_port", 0))
                remote_port = int(payload.get("remote_port", 0))
                ns = payload.get("namespace") or self.server.namespace
                start_now = bool(payload.get("start", True))
                fwd = PortForward(
                    name=name,
                    namespace=ns,
                    target=target,
                    local_port=local_port,
                    remote_port=remote_port,
                )
                self.server.supervisor.add_or_update(fwd, persist=True)
                if start_now:
                    self.server.supervisor.start(name)
                self._json(200, {"ok": True, "forward": fwd.__dict__})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if self.path == "/v1/forwards/delete":
            if self.server.supervisor is None:
                self._json(409, {"error": "forward-supervision-disabled"})
                return
            name = payload.get("name")
            if not isinstance(name, str):
                self._json(400, {"error": "invalid-name"})
                return
            try:
                self.server.supervisor.remove(name, persist=True)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if self.path == "/v1/releases/promote":
            if not self.server.catalog:
                self._json(400, {"error": "catalog-not-configured"})
                return
            src = payload.get("source_name")
            tgt = payload.get("target_name")
            if not src or not tgt:
                self._json(400, {"error": "source_name and target_name required"})
                return
            try:
                rec = promote_release(self.server.catalog, src, tgt)
                self._json(200, {"ok": True, "promoted": rec.name, "artifact_digest": rec.source.artifact_digest})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if self.path == "/v1/releases/rollback":
            if not self.server.workflow:
                self._json(400, {"error": "workflow-not-configured"})
                return
            tgt = payload.get("target_release_name")
            if not tgt:
                self._json(400, {"error": "target_release_name required"})
                return
            try:
                self.server.workflow.catalog.select(tgt)
                self._json(200, {"ok": True, "rolled_back_to": tgt})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if self.path == "/v1/artifacts/gc":
            inv = self.server.artifact_inventory
            if not inv:
                self._json(200, {"pruned_digests": [], "freed_bytes": 0, "dry_run": True})
                return
            dry_run = payload.get("dry_run", True)
            ttl = payload.get("retention_ttl_seconds", 86400)
            gc = SafeGarbageCollector(retention_ttl_seconds=ttl)
            try:
                receipt = gc.run_gc(inv, dry_run=dry_run)
                self._json(200, receipt.to_dict())
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if self.path == "/v1/backup/create":
            if not self.server.state_store:
                self._json(400, {"error": "state-store-not-configured"})
                return
            dest = self.server.state_store.base_dir / f"backup-{int(time.time())}.tar.gz"
            try:
                res = self.server.state_store.create_backup(dest)
                self._json(200, {"ok": True, "backup_path": str(res)})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        self._json(404, {"error": "not-found"})
