"""Self-contained XSS-safe HTML5 Drift Console dashboard renderer."""

from __future__ import annotations

import html
from typing import Any

from pydantic import BaseModel, Field


class DashboardSnapshot(BaseModel, frozen=True):
    """Immutable snapshot of tenant drift telemetry and quota state for the Drift Console."""

    org_id: str
    plan: str
    requests_per_minute_limit: int
    llm_calls_limit: int
    total_events: int
    drifted_events: int
    unique_fingerprints: int
    prs_opened: int
    recent_events: list[dict[str, Any]] = Field(default_factory=list)


def _esc(value: object) -> str:
    """Escape any dynamic value for safe HTML5 text and attribute interpolation."""
    return html.escape(str(value), quote=True)


def _format_event_field_summary(event: dict[str, Any]) -> str:
    """Summarize redacted drift field metadata from an event record."""
    parts: list[str] = []
    missing = event.get("missing_fields")
    if isinstance(missing, list) and missing:
        joined = ", ".join(_esc(item) for item in missing)
        parts.append(f"missing: [{joined}]")
    extra = event.get("extra_fields")
    if isinstance(extra, list) and extra:
        joined = ", ".join(_esc(item) for item in extra)
        parts.append(f"extra: [{joined}]")
    detail = event.get("detail") or event.get("summary")
    if detail and not parts:
        parts.append(_esc(detail))
    if not parts and "issue_count" in event:
        kinds = event.get("issue_kinds")
        if isinstance(kinds, (list, tuple)) and kinds:
            joined = ", ".join(_esc(k) for k in kinds)
            parts.append(f"{_esc(event.get('issue_count', 0))} issues ({joined})")
        else:
            parts.append(f"{_esc(event.get('issue_count', 0))} issues")
    return "; ".join(parts) if parts else "—"


def _render_event_row(event: dict[str, Any]) -> str:
    """Render a single redacted drift event as an escaped HTML table row."""
    timestamp = _esc(event.get("timestamp") or event.get("created_at") or "—")
    model_name = _esc(event.get("model_name") or "unknown")
    fingerprint = _esc(event.get("fingerprint") or "—")
    drifted = bool(event.get("drifted", event.get("has_drift", False)))
    status_class = "status-drifted" if drifted else "status-ok"
    status_label = "DRIFTED" if drifted else "OK"
    pr_value = event.get("pr_url") or event.get("pr_number") or event.get("pr_opened")
    if pr_value is True:
        pr_cell = '<span class="badge badge-pr">Opened</span>'
    elif isinstance(pr_value, str) and pr_value.startswith(("https://", "http://")):
        safe_url = _esc(pr_value)
        pr_cell = (
            f'<a class="pr-link mono" href="{safe_url}" '
            'target="_blank" rel="noopener noreferrer">'
            f"{safe_url}</a>"
        )
    elif pr_value:
        pr_cell = f'<span class="mono">{_esc(pr_value)}</span>'
    else:
        pr_cell = "—"
    fields_summary = _format_event_field_summary(event)

    return (
        "<tr>"
        f'<td class="mono">{timestamp}</td>'
        f'<td class="mono">{model_name}</td>'
        f'<td class="mono">{fingerprint}</td>'
        f'<td><span class="badge {status_class}">{status_label}</span></td>'
        f"<td>{fields_summary}</td>"
        f"<td>{pr_cell}</td>"
        "</tr>"
    )


def render_dashboard_html(snapshot: DashboardSnapshot) -> str:
    """Render a self-contained, responsive, XSS-safe HTML5 Drift Console page."""
    org_id = _esc(snapshot.org_id)
    plan = _esc(snapshot.plan)
    plan_slug = _esc(snapshot.plan.strip().lower().replace(" ", "-"))
    rpm_limit = _esc(snapshot.requests_per_minute_limit)
    llm_limit = _esc(snapshot.llm_calls_limit)
    total_events = _esc(snapshot.total_events)
    drifted_events = _esc(snapshot.drifted_events)
    unique_fingerprints = _esc(snapshot.unique_fingerprints)
    prs_opened = _esc(snapshot.prs_opened)
    drift_rate = (
        f"{(snapshot.drifted_events / snapshot.total_events) * 100:.1f}%"
        if snapshot.total_events > 0
        else "0.0%"
    )

    if snapshot.recent_events:
        rows_html = "\n".join(_render_event_row(event) for event in snapshot.recent_events)
    else:
        rows_html = (
            '<tr><td colspan="6" class="empty-state">'
            "No recent redacted drift events recorded for this organization."
            "</td></tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>SelfHealAPI Drift Console — {org_id}</title>
  <style>
    :root {{
      --bg: #09090b;
      --surface: #111114;
      --surface-alt: #18181b;
      --border: rgba(255, 255, 255, 0.09);
      --text: #fafafa;
      --muted: #a1a1aa;
      --accent: #6366f1;
      --ok-bg: rgba(16, 185, 129, 0.14);
      --ok-fg: #34d399;
      --warn-bg: rgba(245, 158, 11, 0.16);
      --warn-fg: #fbbf24;
      --pr-bg: rgba(99, 102, 241, 0.16);
      --pr-fg: #818cf8;
    }}
    @media (prefers-color-scheme: light) {{
      :root {{
        --bg: #fafafa;
        --surface: #ffffff;
        --surface-alt: #f4f4f5;
        --border: rgba(9, 9, 11, 0.1);
        --text: #09090b;
        --muted: #71717a;
        --accent: #4f46e5;
        --ok-bg: rgba(16, 185, 129, 0.12);
        --ok-fg: #059669;
        --warn-bg: rgba(245, 158, 11, 0.14);
        --warn-fg: #d97706;
        --pr-bg: rgba(79, 70, 229, 0.12);
        --pr-fg: #4f46e5;
      }}
    }}
    .icon {{
      width: 15px;
      height: 15px;
      vertical-align: -2px;
      stroke: currentColor;
      fill: none;
      stroke-width: 1.75;
      stroke-linecap: round;
      stroke-linejoin: round;
    }}
    .pr-link {{ color: var(--pr-fg); text-decoration: none; }}
    .pr-link:hover {{ text-decoration: underline; }}
    .card-value, .mono {{ font-variant-numeric: tabular-nums; }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .container {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 2rem 1.25rem 3rem;
    }}
    .header {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
      padding-bottom: 1.5rem;
      border-bottom: 1px solid var(--border);
    }}
    .title {{
      margin: 0;
      font-size: 1.5rem;
      font-weight: 700;
      letter-spacing: -0.01em;
    }}
    .subtitle {{
      margin: 0.25rem 0 0;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .header-meta {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.75rem;
    }}
    .badge {{
      display: inline-block;
      padding: 0.25rem 0.65rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      letter-spacing: 0.03em;
      text-transform: uppercase;
    }}
    .badge-plan {{
      background: rgba(56, 189, 248, 0.18);
      color: var(--accent);
      border: 1px solid rgba(56, 189, 248, 0.35);
    }}
    .status-drifted {{
      background: var(--warn-bg);
      color: var(--warn-fg);
    }}
    .status-ok {{
      background: var(--ok-bg);
      color: var(--ok-fg);
    }}
    .badge-pr {{
      background: var(--pr-bg);
      color: var(--pr-fg);
    }}
    .quota-bar {{
      display: flex;
      flex-wrap: wrap;
      gap: 1rem;
      margin: 1.25rem 0 1.5rem;
    }}
    .quota-pill {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 0.5rem;
      padding: 0.6rem 0.9rem;
      font-size: 0.85rem;
      color: var(--muted);
    }}
    .quota-pill strong {{
      color: var(--text);
      margin-left: 0.35rem;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 1rem;
      margin-bottom: 2rem;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 0.75rem;
      padding: 1.1rem 1.25rem;
    }}
    .card-label {{
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
    }}
    .card-value {{
      margin-top: 0.4rem;
      font-size: 1.85rem;
      font-weight: 700;
      color: var(--text);
    }}
    .panel {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 0.75rem;
      overflow: hidden;
    }}
    .panel-header {{
      padding: 1rem 1.25rem;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .panel-title {{
      margin: 0;
      font-size: 1rem;
      font-weight: 600;
    }}
    .table-wrap {{
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.875rem;
    }}
    th, td {{
      text-align: left;
      padding: 0.75rem 1rem;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }}
    th {{
      background: var(--surface-alt);
      color: var(--muted);
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      font-weight: 600;
    }}
    tbody tr:last-child td {{
      border-bottom: none;
    }}
    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.82rem;
    }}
    .empty-state {{
      text-align: center;
      color: var(--muted);
      padding: 2rem 1rem;
    }}
  </style>
</head>
<body>
  <main class="container">
    <header class="header">
      <div>
        <h1 class="title">SelfHealAPI Drift Console</h1>
        <p class="subtitle">
          Zero-PII Schema Drift Telemetry &amp; Autonomous Remediation —
          Org: <span class="mono">{org_id}</span>
        </p>
      </div>
      <div class="header-meta">
        <span class="badge badge-plan plan-{plan_slug}">Plan: {plan}</span>
      </div>
    </header>

    <section class="quota-bar" aria-label="Plan Quotas">
      <div class="quota-pill">Rate Limit:<strong>{rpm_limit} req/min</strong></div>
      <div class="quota-pill">LLM Synthesis Quota:<strong>{llm_limit} calls</strong></div>
      <div class="quota-pill">Drift Rate:<strong>{drift_rate}</strong></div>
    </section>

    <section class="grid" aria-label="Drift Metrics">
      <article class="card">
        <div class="card-label">Total Events</div>
        <div class="card-value">{total_events}</div>
      </article>
      <article class="card">
        <div class="card-label">Drifted Events</div>
        <div class="card-value">{drifted_events}</div>
      </article>
      <article class="card">
        <div class="card-label">Unique Fingerprints</div>
        <div class="card-value">{unique_fingerprints}</div>
      </article>
      <article class="card">
        <div class="card-label">PRs Opened</div>
        <div class="card-value">{prs_opened}</div>
      </article>
    </section>

    <section class="panel" aria-label="Recent Redacted Drift Events">
      <div class="panel-header">
        <h2 class="panel-title">Recent Redacted Drift Events</h2>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Timestamp</th>
              <th>Model</th>
              <th>Fingerprint</th>
              <th>Status</th>
              <th>Redacted Field Summary</th>
              <th>PR</th>
            </tr>
          </thead>
          <tbody>
            {rows_html}
          </tbody>
        </table>
      </div>
    </section>
  </main>
</body>
</html>
"""
