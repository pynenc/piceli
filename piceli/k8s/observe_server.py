"""Piceli Operator Server: unified versioned REST API and semantic reactive operator UI.

Library, CLI, versioned REST, and UI share identical authorization and operation semantics.
Single-instance state store and file-based backup ensure reliability without distributed consensus.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import subprocess
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from piceli.artifacts.gc import (
    ImageSpaceInventory,
    SafeGarbageCollector,
)
from piceli.k8s.automation import (
    promote_release,
)
from piceli.k8s.observe import (
    ForwardScope,
    ForwardSupervisor,
    PortForward,
    PreferenceStore,
    UserPreferences,
)
from piceli.k8s.operator_state import (
    FileStateStore,
    OperatorUser,
    PolicyStore,
    UserStore,
)
from piceli.k8s.release import ReleaseCatalog, ReleaseWorkflow
from piceli.k8s.ui_config import UiConfig

_LOG = logging.getLogger(__name__)

MAX_BODY_BYTES = 1024 * 1024
MAX_LOG_TAIL = 2000
# Roles allowed to call mutating (POST) endpoints with a bearer token. The
# per-process local token belongs to the user who started the server and is
# treated as admin.
MUTATING_ROLES = frozenset({"admin", "operator"})
_LOCAL_PRINCIPAL = "local"


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
    table.fixed {
      table-layout: fixed;
    }
    table.fixed td {
      overflow: hidden;
    }
    .clip {
      display: block;
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .muted-note { color: var(--muted); font-size: 11px; display: block; }
    .badge-derived { background: rgba(56, 189, 248, 0.08); color: #7dd3fc; border: 1px solid rgba(56, 189, 248, 0.2); }
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
    .badge-starting, .badge-degraded { background: rgba(234, 179, 8, 0.14); color: #eab308; border: 1px solid rgba(234, 179, 8, 0.3); }
    .health-healthy { background: rgba(34, 197, 94, 0.14); color: #22c55e; border: 1px solid rgba(34, 197, 94, 0.3); }
    .health-starting, .health-restarting, .health-unhealthy { background: rgba(234, 179, 8, 0.14); color: #eab308; border: 1px solid rgba(234, 179, 8, 0.3); }
    .health-conflict, .health-failed { background: rgba(239, 68, 68, 0.14); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.3); }
    .health-unknown, .health-stopped { background: rgba(148, 163, 184, 0.1); color: var(--muted); border: 1px solid rgba(148, 163, 184, 0.2); }
    .qc-health { font-size: 11px; color: var(--muted); margin-top: 6px; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }

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
      opacity: 0.6;
      background: var(--card);
      color: var(--muted);
      border-color: var(--border);
      cursor: pointer;
    }
    a.btn-disabled:hover {
      opacity: 0.9;
      color: var(--text);
      border-color: var(--accent);
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
    .log-terminal[data-size="small"] { font-size: 11px; line-height: 1.35; }
    .log-terminal[data-size="normal"] { font-size: 12px; line-height: 1.55; }
    .log-terminal[data-size="large"] { font-size: 14px; line-height: 1.7; }
    .pod-selector-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
      gap: 6px;
      flex: 1 1 520px;
      min-width: 320px;
      max-height: 190px;
      overflow-y: auto;
      padding-right: 4px;
    }
    .pod-choice {
      display: grid;
      grid-template-columns: auto 8px minmax(0, 1fr) auto;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 6px;
      cursor: pointer;
      font-size: 12px;
      min-width: 0;
    }
    .pod-choice:hover {
      border-color: var(--pod-color, var(--border));
    }
    .pod-choice span:nth-child(3) {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .log-line {
      display: grid;
      grid-template-columns: auto minmax(112px, 150px) minmax(0, 1fr);
      gap: 6px;
      padding: 1px 0;
      border-bottom: 1px solid rgba(255,255,255,0.03);
    }
    .log-badge {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      padding: 1px 6px;
      border-radius: 4px;
      font-size: 10px;
      font-weight: 600;
      color: #fff;
      text-align: center;
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
      <span class="brand-tag">Piceli Observe</span>
    </div>
    <div class="header-actions">
      <div id="hdr-badges" style="display: contents;"></div>
      <div class="status-pill" id="hdr-ns" data-testid="badge-namespace">Namespace: loading...</div>
      <button class="btn btn-sec" id="btn-toggle-refresh" data-testid="btn-autorefresh-toggle" data-action="toggleAutoRefresh">Auto-Refresh: ON (3s)</button>
      <button class="btn btn-sec" data-testid="btn-refresh" data-action="refresh">↻ Refresh</button>
    </div>
  </header>

  <nav>
    <button class="active" data-testid="nav-inventory" data-action="tab" data-tab="inventory">Overview & Inventory</button>
    <button data-testid="nav-forwards" data-action="tab" data-tab="forwards">Port Forwards & Shortcuts</button>
    <button data-testid="nav-releases" data-action="tab" data-tab="releases">Releases & History</button>
    <button data-testid="nav-automation" data-action="tab" data-tab="automation">Git & PR Automation</button>
    <button data-testid="nav-artifacts" data-action="tab" data-tab="artifacts">Artifacts & Safe GC</button>
    <button data-testid="nav-logs" data-action="tab" data-tab="logs">Workload Logs</button>
    <button data-testid="nav-state" data-action="tab" data-tab="state">State & Backup</button>
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

      <div class="section-box" data-testid="section-deployment-plan">
        <h2>Desired vs Live Deployment Plan</h2>
        <table class="fixed">
          <colgroup>
            <col style="width: 110px"><col style="width: 13%"><col style="width: 24%"><col style="width: 26%"><col>
          </colgroup>
          <thead>
            <tr><th>Action</th><th>Kind</th><th>Name</th><th>Reason</th><th>Image / Phase</th></tr>
          </thead>
          <tbody id="tbl-plan" data-testid="tbl-plan"><tr><td colspan="5" style="color: var(--muted);">Loading plan...</td></tr></tbody>
        </table>
      </div>

      <!-- System Topology & Architecture Tiers View -->
      <div class="section-box" data-testid="section-system-topology">
        <h2>
          <span>🏛️ System Topology & Component Architecture</span>
          <span id="topology-subtitle" style="font-size: 11px; font-weight: normal; color: var(--muted);"></span>
        </h2>
        <div class="topology-grid" id="topology-container" data-testid="topology-grid">
          <div style="color: var(--muted); font-size: 12px;">Loading topology...</div>
        </div>
      </div>

      <div class="section-box">
        <h2>Managed Resources (Piceli Declared)</h2>
        <table class="fixed">
          <colgroup>
            <col style="width: 14%"><col style="width: 28%"><col style="width: 100px"><col style="width: 100px"><col>
          </colgroup>
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
            <tr><th>Name</th><th>Namespace</th><th>Target</th><th>Local Port</th><th>Remote Port</th><th>Live Web Link</th><th>Health</th><th>Status</th><th>Actions</th></tr>
          </thead>
          <tbody id="tbl-forwards" data-testid="tbl-forwards"><tr><td colspan="9" style="color: var(--muted);">No forwards active</td></tr></tbody>
        </table>
      </div>

      <div class="section-box">
        <h2>Add Custom Port Forward</h2>
        <form class="form-inline" id="form-add-forward" data-testid="form-add-forward">
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
          <button class="btn" data-testid="btn-promote-release" data-action="promote">Promote Digest (No Rebuild)</button>
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
          <button class="btn btn-danger" data-testid="btn-run-gc" data-action="gc">Run Safe GC</button>
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

    <!-- TAB 6: MULTI-POD LOGS -->
    <div id="tab-logs" class="tab-pane">
      <div class="section-box">
        <h2>Multi-Pod Log Streaming</h2>
        <div style="display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap;">
          <div id="pod-selector" data-testid="pod-selector" class="pod-selector-grid">
            <div style="color: var(--muted); font-size: 12px;">Loading pods...</div>
          </div>
          <div style="display: flex; gap: 8px; align-items: center; flex-shrink: 0;">
            <button class="btn btn-sec" data-testid="btn-select-all" data-action="selectPods" data-state="true" style="font-size: 11px; padding: 4px 10px;">Select All</button>
            <button class="btn btn-sec" data-testid="btn-deselect-all" data-action="selectPods" data-state="false" style="font-size: 11px; padding: 4px 10px;">Deselect All</button>
            <select id="log-tail-multi" data-testid="select-log-tail" style="padding: 5px 8px; background: var(--card); border: 1px solid var(--border); color: var(--text); border-radius: 6px; font-size: 12px;">
              <option value="50">50 lines</option>
              <option value="100" selected>100 lines</option>
              <option value="250">250 lines</option>
              <option value="500">500 lines</option>
            </select>
            <button class="btn" data-testid="btn-fetch-multi-logs" data-action="fetchLogs">Fetch Logs</button>
            <button class="btn btn-sec" id="btn-live-toggle" data-testid="btn-live-toggle" data-action="live">&#9654; Live</button>
            <button class="btn btn-sec" data-testid="btn-log-smaller" data-action="logSize" data-delta="-1">A-</button>
            <button class="btn btn-sec" data-testid="btn-log-larger" data-action="logSize" data-delta="1">A+</button>
          </div>
        </div>
        <div style="margin-top: 8px;">
          <input type="text" id="log-filter" data-testid="input-log-filter" placeholder="Filter logs..." style="width: 100%; padding: 6px 10px; background: var(--card); border: 1px solid var(--border); color: var(--text); border-radius: 6px; font-size: 12px;">
        </div>
        <div class="log-terminal" id="multi-log-output" data-testid="multi-log-output" data-size="normal" style="margin-top: 10px; max-height: 600px; overflow-y: auto;">Logs will appear here...</div>
      </div>
    </div>

    <!-- TAB 7: STATE & BACKUP -->
    <div id="tab-state" class="tab-pane">
      <div class="section-box">
        <h2>State Store & Single-Instance Safety</h2>
        <p style="color: var(--muted);">State is persisted atomically to volume storage with <code>0o600</code> permissions and <code>fcntl.flock</code> locking.</p>
        <button class="btn" data-testid="btn-create-backup" data-action="backup">Create Backup Archive (.tar.gz)</button>
        <div id="backup-status" style="margin-top: 14px;"></div>
      </div>
    </div>
  </main>

  <div id="toasts" class="toast-container"></div>

  <script nonce="__PICELI_NONCE__">
    const WORKLOAD_KINDS = new Set(['Deployment', 'StatefulSet', 'DaemonSet', 'Job', 'CronJob']);

    // Configured tiers win; otherwise group live workloads by the
    // app.kubernetes.io/component label (falling back to app).
    function topologyTiers(status) {
      if ((UI.tiers || []).length) return UI.tiers;
      const groups = new Map();
      (status.managed || []).forEach(r => {
        if (!WORKLOAD_KINDS.has(r.ref?.kind)) return;
        const labels = r.observed?.labels || {};
        const group = labels['app.kubernetes.io/component'] || labels['app'] || 'ungrouped';
        if (!groups.has(group)) groups.set(group, []);
        groups.get(group).push({ name: r.ref.name, role: r.ref.kind, description: '', ports: '', shortcut: null });
      });
      return Array.from(groups.entries()).sort((a, b) => a[0].localeCompare(b[0])).map(([name, components]) => ({
        id: name.toLowerCase().replace(/[^a-z0-9]+/g, '-'), name, badge: '', components
      }));
    }

    function renderStaticConfig() {
      document.getElementById('topology-subtitle').textContent =
        UI.topology_subtitle || ((UI.tiers || []).length ? '' : 'Grouped by app.kubernetes.io/component label');
      document.getElementById('hdr-badges').innerHTML = (UI.badges || []).map(b => `
        <div class="status-pill" data-testid="badge-config" style="border-color: rgba(34, 197, 94, 0.4); background: rgba(34, 197, 94, 0.08); color: #86efac;">
          <span><strong>${esc(b.label)}:</strong> ${esc(b.text)}</span>
        </div>`).join('');
    }

    const ACTIONS = {
      toggleAutoRefresh: () => toggleAutoRefresh(),
      refresh: () => loadAll(),
      tab: el => switchTab(el.dataset.tab, el),
      promote: () => promoteRelease(),
      gc: () => runGC(),
      selectPods: el => toggleAllPods(el.dataset.state === 'true'),
      fetchLogs: () => fetchMultiLogs(),
      live: () => toggleLiveStream(),
      logSize: el => setLogSize(parseInt(el.dataset.delta, 10) || 0),
      backup: () => createBackup(),
      shortcut: el => actQuickShortcut(el.dataset.id, el.dataset.op),
      logsFor: el => viewLogsFor(el.dataset.target),
      rollback: el => rollbackRelease(el.dataset.name),
      forward: el => actForward(el.dataset.name, el.dataset.op),
      deleteForward: el => deleteForward(el.dataset.name),
    };

    // One delegated listener replaces inline handlers so the CSP needs no 'unsafe-inline' for scripts.
    document.addEventListener('click', ev => {
      const el = ev.target.closest('[data-action]');
      if (!el || !Object.prototype.hasOwnProperty.call(ACTIONS, el.dataset.action)) return;
      ev.preventDefault();
      ACTIONS[el.dataset.action](el);
    });
    document.getElementById('form-add-forward').addEventListener('submit', ev => {
      ev.preventDefault();
      addCustomForward();
    });

    function viewLogsFor(target) {
      const podName = target.replace(/^(deployment|pod)\\//, '');
      document.querySelectorAll('.pod-checkbox').forEach(cb => {
        cb.checked = cb.value.startsWith(podName);
      });
      switchTab('logs', document.querySelector('[data-testid="nav-logs"]'));
      fetchMultiLogs();
    }

    const token = __PICELI_TOKEN__;
    const PAGE = __PICELI_PAGE__;
    const UI = PAGE.ui || {};
    let autoRefreshActive = true;
    let autoRefreshTimer = null;
    let currentNamespace = PAGE.namespace || '';

    // HTML-escape for text and quoted attribute contexts. User-controlled values are
    // never placed inside inline JS; actions read them back from data-* attributes.
    const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    // "registry/path/name@sha256:<64 hex>" -> "name@sha256:0123456789ab…"; the
    // full reference stays in the title tooltip, so the cell never overflows.
    const shortImage = ref => {
      const text = String(ref ?? '');
      const at = text.indexOf('@sha256:');
      const repo = at >= 0 ? text.slice(0, at) : text;
      const name = repo.split('/').pop() || repo;
      return at >= 0 ? `${name}@sha256:${text.slice(at + 8, at + 20)}…` : name;
    };
    const imageCells = images => images.map(ref =>
      `<code class="clip" title="${esc(ref)}">${esc(shortImage(ref))}</code>`).join('');
    const LIVE_STATES = ['running', 'degraded', 'starting'];
    function healthBadge(item, testid) {
      const health = item.health || 'unknown';
      const lines = [];
      if (item.last_error) lines.push('last error: ' + item.last_error);
      if (item.last_probe_at) lines.push('last probe: ' + item.last_probe_at);
      lines.push('restarts: ' + (item.restarts || 0));
      if (item.probe) lines.push('probe: ' + item.probe.type + (item.probe.path ? ' ' + item.probe.path : '') + ' every ' + item.probe.interval + 's');
      const restarts = item.restarts ? ' \u21bb' + item.restarts : '';
      return `<span class="badge health-${esc(health)}" data-testid="${esc(testid)}" title="${esc(lines.join('\\n'))}">${esc(health)}${esc(restarts)}</span>`;
    }
    const TIER_BADGES = ['tier-badge-core', 'tier-badge-task', 'tier-badge-app', 'tier-badge-sensor'];
    const POD_COLORS = ['#38bdf8','#22c55e','#eab308','#a855f7','#ef4444','#f97316','#06b6d4','#ec4899','#14b8a6','#6366f1'];
    const LOG_SIZES = ['small', 'normal', 'large'];

    function colorForName(name) {
      let hash = 0;
      for (const ch of String(name || '')) hash = ((hash << 5) - hash + ch.charCodeAt(0)) | 0;
      return POD_COLORS[Math.abs(hash) % POD_COLORS.length];
    }

    function setLogSize(delta) {
      const el = document.getElementById('multi-log-output');
      if (!el) return;
      const current = LOG_SIZES.indexOf(el.dataset.size || 'normal');
      const next = Math.max(0, Math.min(LOG_SIZES.length - 1, current + delta));
      el.dataset.size = LOG_SIZES[next];
    }

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

        currentNamespace = status.namespace || PAGE.namespace || '';
        document.getElementById('hdr-ns').textContent = 'Namespace: ' + (currentNamespace || '(unset)');
        const addNsInput = document.getElementById('add-fwd-ns');
        if (addNsInput && !addNsInput.value) addNsInput.value = currentNamespace;

        document.getElementById('m-managed').textContent = (status.managed || []).length;
        document.getElementById('m-unmanaged').textContent = (status.unmanaged || []).length;
        document.getElementById('m-unknown').textContent = (status.unknown || []).length;
        document.getElementById('m-release').textContent = status.active_release || 'None';

        const planRows = [];
        (status.managed || []).forEach(r => {
          const state = r.state || 'unknown';
          const derived = r.derived_from;
          const action = derived ? 'derived' : (state === 'present' ? 'no-op' : (state === 'missing' ? 'create' : 'inspect'));
          const reason = derived ? 'created by ' + derived : (state === 'present' ? 'declared and live' : (state === 'missing' ? 'declared but absent' : (r.error || 'reader could not prove state')));
          planRows.push({
            action,
            kind: r.ref?.kind || '',
            name: r.ref?.name || '',
            reason,
            images: r.observed?.images || [],
            detail: r.observed?.phase || '-'
          });
        });
        (status.unmanaged || []).forEach(r => {
          planRows.push({
            action: 'unmanaged',
            kind: r.ref?.kind || '',
            name: (r.ref?.namespace ? r.ref.namespace + '/' : '') + (r.ref?.name || ''),
            reason: 'live object outside selected deployment archive',
            images: r.observed?.images || [],
            detail: r.observed?.phase || '-'
          });
        });
        (status.unknown || []).forEach(r => {
          planRows.push({
            action: 'inspect',
            kind: r.ref?.kind || '',
            name: r.ref?.name || '',
            reason: r.error || 'unknown live state',
            images: [],
            detail: '-'
          });
        });
        document.getElementById('tbl-plan').innerHTML = planRows.map(row => `
          <tr>
            <td><span class="badge badge-${esc(row.action === 'no-op' ? 'present' : row.action === 'create' ? 'unknown' : row.action)}">${esc(row.action)}</span></td>
            <td><span class="clip" title="${esc(row.kind)}">${esc(row.kind)}</span></td>
            <td><code class="clip" title="${esc(row.name)}">${esc(row.name)}</code></td>
            <td>${esc(row.reason)}</td>
            <td>${row.images.length ? imageCells(row.images) : `<code class="clip">${esc(row.detail)}</code>`}</td>
          </tr>
        `).join('') || '<tr><td colspan="5" style="color:var(--muted);">No deployment plan available</td></tr>';

        // Render Quick Shortcuts Cards
        const shortcuts = shortcutsData.shortcuts || [];
        const shortcutsHtml = shortcuts.map(sc => {
          const isRunning = LIVE_STATES.includes(sc.state);
          const isHealthy = (sc.state === 'running');
          const isBackoff = (sc.state === 'backoff' || sc.state === 'degraded' || sc.state === 'starting');
          const isFailed = (sc.state === 'failed');
          const pulseClass = isHealthy ? 'pulse-running' : (isBackoff ? 'pulse-backoff' : (isFailed ? 'pulse-failed' : 'pulse-stopped'));
          const stateLabel = isRunning ? `${sc.state} (PID ${sc.pid})` : (sc.state || 'stopped');

          return `
            <div class="quick-card" data-testid="shortcut-card-${esc(sc.id)}">
              <div class="qc-top">
                <div>
                  <div class="qc-name">${esc(sc.label)}</div>
                  <div class="qc-desc">${esc(sc.description)}</div>
                  <div class="qc-route">${esc(sc.target)} → 127.0.0.1:${esc(sc.local_port)}</div>
                  <div class="qc-health">Health ${healthBadge(sc, 'health-' + sc.id)}${sc.last_error && sc.health !== 'healthy' ? `<span data-testid="health-error-${esc(sc.id)}">${esc(sc.last_error)}</span>` : ''}</div>
                </div>
                <span class="badge badge-${esc(sc.state)}">
                  <span class="pulse-dot ${pulseClass}"></span>${esc(stateLabel)}
                </span>
              </div>
              <div class="qc-actions">
                ${isRunning ? `
                  <button class="btn btn-danger" data-testid="btn-stop-${esc(sc.id)}" data-action="shortcut" data-id="${esc(sc.id)}" data-op="stop">Stop</button>
                  <a href="${esc(sc.url)}" target="_blank" data-testid="link-open-${esc(sc.id)}" class="btn btn-open">Open ↗</a>
                ` : `
                  <button class="btn" data-testid="btn-start-${esc(sc.id)}" data-action="shortcut" data-id="${esc(sc.id)}" data-op="start">▶ Start Forward</button>
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

        const topologyHtml = topologyTiers(status).map((tier, index) => `
          <div class="tier-group" data-testid="tier-group-${esc(tier.id)}">
            <div class="tier-header">
              <div class="tier-title">${esc(tier.name)}</div>
              ${tier.badge ? `<span class="tier-badge ${TIER_BADGES[index % TIER_BADGES.length]}">${esc(tier.badge)}</span>` : ''}
            </div>
            ${tier.components.map(comp => {
              const res = managedMap.get(comp.name);
              const isPresent = res && res.state === 'present';
              const phase = res?.observed?.phase || (isPresent ? 'Running' : 'Not Deployed');
              const images = (res?.observed?.images || []).join(', ') || 'canonical';
              const shortcut = comp.shortcut ? shortcuts.find(s => s.id === comp.shortcut) : null;
              const compUrl = shortcut ? shortcut.url : '';
              const fwdRunning = shortcut && LIVE_STATES.includes(shortcut.state);

              return `
                <div class="comp-item" data-testid="topology-card-${esc(comp.name)}">
                  <div class="comp-header">
                    <span class="comp-name">${esc(comp.name)}</span>
                    <span class="badge ${isPresent ? 'badge-running' : 'badge-stopped'}">
                      <span class="pulse-dot ${isPresent ? 'pulse-running' : 'pulse-stopped'}"></span>${esc(phase)}
                    </span>
                  </div>
                  <div class="comp-desc">${esc(comp.description)}</div>
                  <div class="comp-meta">
                    ${comp.role ? `<span class="comp-tag">Role: <strong>${esc(comp.role)}</strong></span>` : ''}
                    ${comp.ports ? `<span class="comp-tag">Port: <code>${esc(comp.ports)}</code></span>` : ''}
                  </div>
                  <div class="comp-actions">
                    ${shortcut ? (fwdRunning ? `
                      <button class="btn btn-danger" style="padding: 3px 8px; font-size: 11px;" data-action="shortcut" data-id="${esc(comp.shortcut)}" data-op="stop">■ Stop</button>
                      <a href="${esc(compUrl)}" target="_blank" class="btn btn-open" style="padding: 3px 8px; font-size: 11px;">Open ↗</a>
                    ` : `
                      <button class="btn" style="padding: 3px 8px; font-size: 11px;" data-action="shortcut" data-id="${esc(comp.shortcut)}" data-op="start">▶ Forward</button>
                      <a href="${esc(compUrl)}" target="_blank" class="btn btn-disabled" style="padding: 3px 8px; font-size: 11px;">Open ↗</a>
                    `) : ''}
                    <button class="btn btn-sec" style="padding: 3px 8px; font-size: 11px;" data-action="logsFor" data-target="deployment/${esc(comp.name)}">Logs ↗</button>
                  </div>
                </div>
              `;
            }).join('')}
          </div>
        `).join('');
        document.getElementById('topology-container').innerHTML = topologyHtml || '<div style="color: var(--muted); font-size: 12px;">No workloads to group</div>';

        // Managed table
        document.getElementById('tbl-managed').innerHTML = (status.managed || []).map(r => `
          <tr data-testid="row-managed-${esc(r.ref.name)}">
            <td>${esc(r.ref.kind)}</td>
            <td><code class="clip" title="${esc(r.ref.name)}">${esc(r.ref.name)}</code>${r.derived_from ? `<span class="muted-note">via ${esc(r.derived_from)}</span>` : ''}</td>
            <td><span class="badge badge-${esc(r.state)}">${esc(r.state)}</span></td>
            <td>${esc(r.observed?.phase || '-')}</td>
            <td>${(r.observed?.images || []).length ? imageCells(r.observed.images) : '<code>-</code>'}</td>
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
              <button class="btn btn-sec" data-action="rollback" data-name="${esc(rel.name)}">Rollback</button>
            </td>
          </tr>
        `).join('') || '<tr><td colspan="6" style="color:var(--muted);">No releases catalogued</td></tr>';

        // Forwards table
        document.getElementById('tbl-forwards').innerHTML = (forwards.forwards || []).map(f => {
          const isRunning = LIVE_STATES.includes(f.state);
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
              <td>${healthBadge(f, 'health-fwd-' + f.name)}</td>
              <td><span class="badge badge-${esc(f.state)}">${esc(f.state)}${f.pid ? ' (' + f.pid + ')' : ''}</span></td>
              <td>
                ${isRunning ? `
                  <button class="btn btn-danger" data-testid="btn-stop-fwd-${esc(f.name)}" data-action="forward" data-name="${esc(f.name)}" data-op="stop">Stop</button>
                ` : `
                  <button class="btn" data-testid="btn-start-fwd-${esc(f.name)}" data-action="forward" data-name="${esc(f.name)}" data-op="start">Start</button>
                `}
                <button class="btn btn-sec" data-testid="btn-del-fwd-${esc(f.name)}" data-action="deleteForward" data-name="${esc(f.name)}">✕</button>
              </td>
            </tr>
          `;
        }).join('') || '<tr><td colspan="9" style="color:var(--muted);">No forwards supervised. Use shortcuts above or add one below.</td></tr>';

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

    let liveStreamTimer = null;
    let liveStreamActive = false;
    let userScrolledUp = false;

    async function loadPods() {
      try {
        const res = await req('/v1/pods');
        const container = document.getElementById('pod-selector');
        if (!container) return;
        const pods = res.pods || [];
        if (pods.length === 0) {
          container.innerHTML = '<div style="color: var(--muted); font-size: 12px;">No pods found in namespace</div>';
          return;
        }
        container.innerHTML = pods.map((p, i) => {
          const color = colorForName(p.name);
          const phaseClass = p.phase === 'Running' ? 'badge-present' : 'badge-unknown';
          return `<label class="pod-choice" data-testid="pod-chk-${esc(p.name)}" style="--pod-color: ${color};">`+
            `<input type="checkbox" class="pod-checkbox" value="${esc(p.name)}" data-color="${color}" checked style="accent-color: ${color};">`+
            `<span style="width: 8px; height: 8px; border-radius: 50%; background: ${color}; display: inline-block;"></span>`+
            `<span>${esc(p.name)}</span>`+
            `<span class="badge ${phaseClass}" style="font-size: 10px; padding: 1px 6px;">${esc(p.phase)}</span>`+
          `</label>`;
        }).join('');
      } catch(e) { /* ignore */ }
    }

    function toggleAllPods(state) {
      document.querySelectorAll('.pod-checkbox').forEach(cb => cb.checked = state);
    }

    function getSelectedPods() {
      return Array.from(document.querySelectorAll('.pod-checkbox:checked')).map(cb => cb.value);
    }

    async function fetchMultiLogs() {
      const pods = getSelectedPods();
      if (pods.length === 0) return showToast('Select at least one pod', 'error');
      const tail = document.getElementById('log-tail-multi').value;
      const res = await req('/v1/logs/multi?pods=' + encodeURIComponent(pods.join(',')) + '&tail=' + tail);
      renderMultiLogs(res.lines || []);
    }

    function renderMultiLogs(lines) {
      const filter = (document.getElementById('log-filter').value || '').toLowerCase();
      const el = document.getElementById('multi-log-output');
      if (!el) return;
      const wasAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
      if (lines.length === 0) {
        el.innerHTML = '<div style="color: var(--muted);">No log lines returned.</div>';
        return;
      }
      const html = lines
        .filter(l => !filter || (l.msg && l.msg.toLowerCase().includes(filter)) || (l.pod && l.pod.toLowerCase().includes(filter)))
        .map(l => {
          const ts = l.ts ? `<span style="color: var(--muted); margin-right: 6px;">${esc(l.ts.substring(11, 23))}</span>` : '';
          const color = colorForName(l.pod);
          const badge = `<span class="log-badge" style="background: ${color}40; border: 1px solid ${color}80;">${esc(l.badge)}</span>`;
          return `<div class="log-line">${ts}${badge}<span>${esc(l.msg)}</span></div>`;
        }).join('');
      el.innerHTML = html;
      if (wasAtBottom && !userScrolledUp) el.scrollTop = el.scrollHeight;
    }

    function toggleLiveStream() {
      liveStreamActive = !liveStreamActive;
      const btn = document.getElementById('btn-live-toggle');
      if (liveStreamActive) {
        btn.innerHTML = '&#9208; Pause';
        btn.classList.remove('btn-sec');
        btn.classList.add('btn-danger');
        fetchMultiLogs();
        liveStreamTimer = setInterval(fetchMultiLogs, 2000);
        showToast('Live log streaming started (2s refresh)', 'success');
      } else {
        btn.innerHTML = '&#9654; Live';
        btn.classList.remove('btn-danger');
        btn.classList.add('btn-sec');
        if (liveStreamTimer) { clearInterval(liveStreamTimer); liveStreamTimer = null; }
        showToast('Live log streaming paused', 'info');
      }
    }

    document.addEventListener('DOMContentLoaded', () => {
      const logEl = document.getElementById('multi-log-output');
      if (logEl) logEl.addEventListener('scroll', () => {
        userScrolledUp = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight > 50;
      });
    });

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

    renderStaticConfig();
    loadAll();
    loadPods();
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
        kubeconfig: Path | None = None,
        kubectl: str = "kubectl",
        context: str | None = None,
        ui_config: UiConfig | None = None,
        preference_scope: ForwardScope | None = None,
    ) -> None:
        if address[0] not in {"127.0.0.1", "::1"}:
            raise ValueError("Piceli observe server must bind to loopback")
        if kubeconfig is not None and not context:
            # kubectl would otherwise fall back to the file's current-context.
            raise ValueError("an explicit kubeconfig context is required")
        self.report = report
        self.preferences = preferences
        #: Only saved forwards for this cluster/context are listed (none without).
        self.preference_scope = preference_scope
        self.supervisor = supervisor
        self.user = user
        self.catalog = catalog
        self.state_store = state_store
        self.workflow = workflow
        self.artifact_inventory = artifact_inventory
        self.log_reader_fn = log_reader_fn
        self.namespace = namespace
        self.kubeconfig = kubeconfig
        self.kubectl = kubectl
        self.context = context
        self.ui_config = ui_config or UiConfig()
        self.local_token = secrets.token_urlsafe(24)
        super().__init__(address, LocalObserveHandler)

    def allowed_hosts(self) -> frozenset[str]:
        """Host header values that name this loopback listener (DNS-rebinding defence)."""
        port = self.server_address[1]
        return frozenset({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})

    def allowed_origins(self) -> frozenset[str]:
        return frozenset(f"http://{host}" for host in self.allowed_hosts())


def _script_json(value: object) -> str:
    """Serialize JSON that is safe to embed inside an inline <script> element."""
    return (
        json.dumps(value, sort_keys=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _csp(nonce: str | None = None) -> str:
    """Content-Security-Policy for the dashboard page (nonce) or API responses (none).

    Scripts run only with the per-response nonce; all handlers are attached by
    delegation, so no inline event attributes are needed.  Styles still allow
    'unsafe-inline' because the page uses many style="" attributes; style
    injection cannot execute script under this policy.
    """
    if nonce is None:
        return "default-src 'none'; frame-ancestors 'none'"
    return (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    )


class _RequestError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class LocalObserveHandler(BaseHTTPRequestHandler):
    """Serve unified versioned REST API and semantic operator UI with identical authorization."""

    server: LocalObserveServer

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep user preferences, tokens, and secret paths out of terminal logs."""

    def _security_headers(self, nonce: str | None = None) -> None:
        self.send_header("Content-Security-Policy", _csp(nonce))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _error(
        self, status: int, code: str, error: BaseException | None = None
    ) -> None:
        """Return a fixed error code; keep exception detail in the server log only."""
        if error is not None:
            _LOG.warning(
                "piceli observe %s %s failed: %s",
                self.command,
                urlparse(self.path).path,
                code,
                exc_info=error,
            )
        self._json(status, {"error": code})

    def _html(self) -> None:
        nonce = secrets.token_urlsafe(16)
        page = {
            "namespace": self.server.namespace,
            "ui": self.server.ui_config.public_dict(),
        }
        body = (
            _PAGE_HTML.replace(
                "__PICELI_TOKEN__", _script_json(self.server.local_token)
            )
            .replace("__PICELI_PAGE__", _script_json(page))
            .replace("__PICELI_NONCE__", nonce)
            .encode()
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers(nonce)
        self.end_headers()
        self.wfile.write(body)

    def _check_origin(self) -> bool:
        """Reject foreign Host (DNS rebinding) and, when present, foreign Origin headers."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.server.allowed_hosts():
            self._error(403, "forbidden-host")
            return False
        origin = self.headers.get("Origin")
        if (
            origin is not None
            and origin.strip().lower() not in self.server.allowed_origins()
        ):
            self._error(403, "forbidden-origin")
            return False
        return True

    def _principal(self) -> str | OperatorUser | None:
        """Return the local principal, an authenticated bearer user, or ``None``."""
        token_hdr = self.headers.get("X-Piceli-Local-Token")
        if token_hdr and secrets.compare_digest(
            token_hdr.encode(), self.server.local_token.encode()
        ):
            return _LOCAL_PRINCIPAL
        auth_hdr = self.headers.get("Authorization")
        if auth_hdr and auth_hdr.startswith("Bearer ") and self.server.state_store:
            token = auth_hdr.split(" ", 1)[1]
            user = UserStore(self.server.state_store).authenticate(token)
            if user:
                return user
        return None

    def _check_auth(self) -> bool:
        """Verify token via X-Piceli-Local-Token or Bearer user store."""
        return self._principal() is not None

    def _read_json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_length)
        except ValueError:
            raise _RequestError(400, "invalid-content-length") from None
        if length < 0:
            raise _RequestError(400, "invalid-content-length")
        if length > MAX_BODY_BYTES:
            raise _RequestError(413, "payload-too-large")
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length))
        except Exception:
            raise _RequestError(400, "invalid-json-payload") from None
        if not isinstance(payload, dict):
            raise _RequestError(400, "invalid-json-payload")
        return payload

    def do_GET(self) -> None:
        if not self._check_origin():
            return
        if self.path == "/":
            self._html()
            return
        if self.path == "/favicon.ico":
            self.send_response(204)
            self._security_headers()
            self.end_headers()
            return
        if self.path == "/healthz":
            self._json(200, {"ok": True})
            return
        if not self._check_auth():
            self._error(401, "unauthorized")
            return
        if self.path == "/v1/status":
            try:
                self._json(200, self.server.report().to_dict())
            except Exception as error:
                self._error(503, "status-unavailable", error)
            return
        if self.path == "/v1/preferences":
            # Only this user's forwards saved for this cluster, context and
            # namespace: other clusters' preferences are private to them.
            scope = self.server.preference_scope
            selected: tuple[UserPreferences, ...] = ()
            if self.server.user and scope is not None:
                users = self.server.preferences.load()
                if self.server.user in users:
                    item = users[self.server.user]
                    selected = (
                        UserPreferences(
                            item.user,
                            tuple(
                                forward
                                for forward in item.forwards
                                if forward.matches(scope, self.server.namespace)
                            ),
                        ),
                    )
            self._json(
                200,
                {
                    "users": [
                        {
                            "user": item.user,
                            "forwards": [
                                forward.public_dict() for forward in item.forwards
                            ],
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
                    records.append(
                        {
                            "name": r.name,
                            "namespace": r.namespace,
                            "kind": r.source.kind,
                            "identity": r.source.identity,
                            "artifact_digest": r.source.artifact_digest,
                            "is_active": (r.name == selected_name),
                        }
                    )
            self._json(200, {"selected": selected_name, "releases": records})
            return
        if self.path == "/v1/artifacts":
            if self.server.artifact_inventory:
                self._json(200, self.server.artifact_inventory.to_dict())
            else:
                self._json(
                    200,
                    {
                        "total_images": 0,
                        "entries": [],
                        "total_bytes": 0,
                        "protected_bytes": 0,
                        "reclaimable_bytes": 0,
                    },
                )
            return
        if self.path == "/v1/automation":
            policies_data = []
            if self.server.state_store:
                ps = PolicyStore(self.server.state_store).load_policies()
                for p in ps.values():
                    policies_data.append(
                        {
                            "name": p.name,
                            "allowed_namespaces": list(p.allowed_namespaces),
                            "allowed_operations": list(p.allowed_operations),
                            "require_pr_approval": p.require_pr_approval,
                        }
                    )
            self._json(200, {"policies": policies_data, "watched_branches": []})
            return
        if self.path == "/v1/pods":
            self._handle_pods()
            return
        if self.path.startswith("/v1/logs/multi"):
            self._handle_logs_multi()
            return
        if self.path.startswith("/v1/logs"):
            if self.server.log_reader_fn:
                try:
                    lines = self.server.log_reader_fn()
                    self._json(200, {"lines": lines})
                except Exception as e:
                    self._error(500, "log-read-failed", e)
            else:
                self._json(200, {"lines": ["Log reader adapter not attached."]})
            return

        self._json(404, {"error": "not-found"})

    def _handle_pods(self) -> None:
        """List running pods in the configured namespace."""
        if not self.server.kubeconfig:
            self._json(200, {"pods": []})
            return
        cmd = [
            self.server.kubectl,
            "--kubeconfig",
            str(self.server.kubeconfig),
        ]
        if self.server.context:
            cmd.extend(["--context", self.server.context])
        newline = chr(10)
        cmd.extend(
            [
                "--namespace",
                self.server.namespace,
                "get",
                "pods",
                "-o",
                f"jsonpath={{range .items[*]}}{{.metadata.name}}|{{.status.phase}}|{{.status.containerStatuses[0].restartCount}}|{{.status.containerStatuses[*].name}}{newline}{{end}}",
            ]
        )
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            pods = []
            for line in result.stdout.strip().splitlines():
                parts = line.split("|")
                if len(parts) >= 2:
                    pods.append(
                        {
                            "name": parts[0],
                            "phase": parts[1],
                            "restarts": parts[2] if len(parts) > 2 else "0",
                            "containers": parts[3] if len(parts) > 3 else "",
                        }
                    )
            self._json(200, {"pods": pods})
        except Exception as e:
            self._error(500, "pod-list-failed", e)

    def _handle_logs_multi(self) -> None:
        """Fetch and merge logs from multiple pods."""
        if not self.server.kubeconfig:
            self._json(200, {"lines": []})
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        pod_names = [
            p.strip() for p in params.get("pods", [""])[0].split(",") if p.strip()
        ]
        try:
            tail = int(params.get("tail", ["100"])[0])
        except ValueError:
            self._json(400, {"error": "invalid-tail"})
            return
        if tail < 1:
            self._json(400, {"error": "invalid-tail"})
            return
        tail = min(tail, MAX_LOG_TAIL)
        if not pod_names:
            self._json(400, {"error": "no pods specified"})
            return
        POD_COLORS = [
            "#38bdf8",
            "#22c55e",
            "#eab308",
            "#a855f7",
            "#ef4444",
            "#f97316",
            "#06b6d4",
            "#ec4899",
            "#14b8a6",
            "#6366f1",
        ]
        all_lines = []
        for idx, pod in enumerate(pod_names[:10]):
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,252}", pod):
                continue
            cmd = [
                self.server.kubectl,
                "--kubeconfig",
                str(self.server.kubeconfig),
            ]
            if self.server.context:
                cmd.extend(["--context", self.server.context])
            cmd.extend(
                [
                    "--namespace",
                    self.server.namespace,
                    "logs",
                    f"pod/{pod}",
                    f"--tail={tail}",
                    "--timestamps=true",
                    "--all-containers=true",
                ]
            )
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                color = POD_COLORS[idx % len(POD_COLORS)]
                short_name = pod.split("-")[0:3]
                badge = "-".join(short_name) if len(short_name) > 1 else pod[:20]
                for raw_line in result.stdout.strip().splitlines():
                    ts = ""
                    msg = raw_line
                    if len(raw_line) > 30 and raw_line[4] == "-":
                        ts = raw_line[:30]
                        msg = raw_line[31:] if len(raw_line) > 31 else ""
                    all_lines.append(
                        {
                            "ts": ts,
                            "pod": pod,
                            "badge": badge,
                            "color": color,
                            "msg": msg,
                        }
                    )
            except Exception:
                all_lines.append(
                    {
                        "ts": "",
                        "pod": pod,
                        "badge": pod[:20],
                        "color": POD_COLORS[idx % len(POD_COLORS)],
                        "msg": f"[error fetching logs from {pod}]",
                    }
                )
        all_lines.sort(key=lambda x: x.get("ts", ""))
        self._json(200, {"lines": all_lines})

    def do_POST(self) -> None:
        if not self._check_origin():
            return
        principal = self._principal()
        if principal is None:
            self._error(401, "unauthorized")
            return
        if isinstance(principal, OperatorUser) and principal.role not in MUTATING_ROLES:
            self._error(403, "forbidden-role")
            return

        try:
            payload = self._read_json_body()
        except _RequestError as error:
            self._json(error.status, {"error": error.code})
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
                self._error(400, "forward-action-failed", e)
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
                shortcuts = self.server.supervisor.shortcuts_status(
                    self.server.namespace
                )
                self._json(
                    200, {"ok": True, "shortcut": shortcut, "shortcuts": shortcuts}
                )
            except Exception as e:
                self._error(400, "shortcut-action-failed", e)
            return

        if self.path == "/v1/forwards/add":
            if self.server.supervisor is None:
                self._json(409, {"error": "forward-supervision-disabled"})
                return
            try:
                name = str(payload.get("name") or "")
                target = str(payload.get("target") or "")
                if not name or not target:
                    self._json(400, {"error": "name-and-target-required"})
                    return
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
                self._json(200, {"ok": True, "forward": fwd.public_dict()})
            except Exception as e:
                self._error(400, "forward-add-failed", e)
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
                self._error(400, "forward-delete-failed", e)
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
                self._json(
                    200,
                    {
                        "ok": True,
                        "promoted": rec.name,
                        "artifact_digest": rec.source.artifact_digest,
                    },
                )
            except Exception as e:
                self._error(400, "promote-failed", e)
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
                self._error(400, "rollback-failed", e)
            return

        if self.path == "/v1/artifacts/gc":
            inv = self.server.artifact_inventory
            if not inv:
                self._json(
                    200, {"pruned_digests": [], "freed_bytes": 0, "dry_run": True}
                )
                return
            dry_run = payload.get("dry_run", True)
            ttl = payload.get("retention_ttl_seconds", 86400)
            gc = SafeGarbageCollector(retention_ttl_seconds=ttl)
            try:
                receipt = gc.run_gc(inv, dry_run=dry_run)
                self._json(200, receipt.to_dict())
            except Exception as e:
                self._error(400, "gc-failed", e)
            return

        if self.path == "/v1/backup/create":
            if not self.server.state_store:
                self._json(400, {"error": "state-store-not-configured"})
                return
            dest = (
                self.server.state_store.base_dir / f"backup-{int(time.time())}.tar.gz"
            )
            try:
                res = self.server.state_store.create_backup(dest)
                self._json(200, {"ok": True, "backup_path": str(res)})
            except Exception as e:
                self._error(500, "backup-failed", e)
            return

        self._json(404, {"error": "not-found"})
