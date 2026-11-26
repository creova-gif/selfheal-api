"""Unit tests for the XSS-safe Drift Console HTML5 dashboard renderer."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from selfheal_api.dashboard import DashboardSnapshot, render_dashboard_html


def test_dashboard_snapshot_is_frozen() -> None:
    """DashboardSnapshot should be immutable (frozen=True)."""
    snapshot = DashboardSnapshot(
        org_id="org_acme",
        plan="pro",
        requests_per_minute_limit=120,
        llm_calls_limit=500,
        total_events=10,
        drifted_events=3,
        unique_fingerprints=2,
        prs_opened=1,
        recent_events=[],
    )
    with pytest.raises(ValidationError):
        snapshot.plan = "enterprise"  # type: ignore[misc]


def test_render_dashboard_html_displays_metrics_plan_and_events() -> None:
    """render_dashboard_html should render plan badge, quotas, metrics, and recent events."""
    snapshot = DashboardSnapshot(
        org_id="org_fin_01",
        plan="growth",
        requests_per_minute_limit=300,
        llm_calls_limit=2000,
        total_events=42,
        drifted_events=7,
        unique_fingerprints=4,
        prs_opened=2,
        recent_events=[
            {
                "timestamp": "2026-11-26T08:00:00Z",
                "model_name": "StripeCharge",
                "fingerprint": "fp_9a8b7c",
                "drifted": True,
                "missing_fields": ["customer_email"],
                "extra_fields": ["billing_email"],
                "pr_url": "https://github.com/acme/repo/pull/18",
            },
            {
                "timestamp": "2026-11-26T07:55:00Z",
                "model_name": "HubSpotContact",
                "fingerprint": "fp_112233",
                "drifted": False,
            },
        ],
    )

    rendered = render_dashboard_html(snapshot)

    assert "<!DOCTYPE html>" in rendered
    assert "SelfHealAPI Drift Console" in rendered
    assert "org_fin_01" in rendered
    assert "Plan: growth" in rendered
    assert "300 req/min" in rendered
    assert "2000 calls" in rendered
    assert ">42<" in rendered
    assert ">7<" in rendered
    assert ">4<" in rendered
    assert ">2<" in rendered
    assert "StripeCharge" in rendered
    assert "fp_9a8b7c" in rendered
    assert "customer_email" in rendered
    assert "billing_email" in rendered
    assert "https://github.com/acme/repo/pull/18" in rendered
    assert "DRIFTED" in rendered
    assert "HubSpotContact" in rendered
    assert ">OK<" in rendered


def test_render_dashboard_html_empty_events_state() -> None:
    """render_dashboard_html should render an empty-state row when no events exist."""
    snapshot = DashboardSnapshot(
        org_id="org_empty",
        plan="free",
        requests_per_minute_limit=30,
        llm_calls_limit=50,
        total_events=0,
        drifted_events=0,
        unique_fingerprints=0,
        prs_opened=0,
    )

    rendered = render_dashboard_html(snapshot)
    assert "No recent redacted drift events recorded" in rendered


def test_render_dashboard_html_escapes_xss_payloads() -> None:
    """All dynamic strings including org_id and model_name must be HTML-escaped."""
    org_xss = '<script>alert("org_xss")</script>'
    model_xss = '<script>alert("model_xss")</script>'
    plan_xss = 'pro" onclick="alert(1)'
    field_xss = '<img src=x onerror="alert(2)">'

    snapshot = DashboardSnapshot(
        org_id=org_xss,
        plan=plan_xss,
        requests_per_minute_limit=60,
        llm_calls_limit=100,
        total_events=1,
        drifted_events=1,
        unique_fingerprints=1,
        prs_opened=0,
        recent_events=[
            {
                "timestamp": "2026-11-26T08:10:00Z",
                "model_name": model_xss,
                "fingerprint": "fp_<xss>",
                "drifted": True,
                "missing_fields": [field_xss],
            }
        ],
    )

    rendered = render_dashboard_html(snapshot)

    assert "<script>" not in rendered
    assert "<img " not in rendered
    assert "&lt;script&gt;alert(&quot;org_xss&quot;)&lt;/script&gt;" in rendered
    assert "&lt;script&gt;alert(&quot;model_xss&quot;)&lt;/script&gt;" in rendered
    assert "pro&quot; onclick=&quot;alert(1)" in rendered
    assert "&lt;img src=x onerror=&quot;alert(2)&quot;&gt;" in rendered


def test_render_dashboard_html_blocks_javascript_pr_urls_and_shows_drift_rate() -> None:
    snapshot = DashboardSnapshot(
        org_id="org_sec",
        plan="pro",
        requests_per_minute_limit=120,
        llm_calls_limit=200,
        total_events=4,
        drifted_events=1,
        unique_fingerprints=1,
        prs_opened=1,
        recent_events=[
            {
                "timestamp": "2026-11-26T10:00:00Z",
                "model_name": "Invoice",
                "fingerprint": "fp_1",
                "drifted": True,
                "pr_url": "javascript:alert(1)",
            },
            {
                "timestamp": "2026-11-26T10:01:00Z",
                "model_name": "Invoice",
                "fingerprint": "fp_2",
                "drifted": False,
                "pr_url": "https://github.com/acme/repo/pull/42",
            },
        ],
    )
    rendered = render_dashboard_html(snapshot)
    assert "Drift Rate:<strong>25.0%</strong>" in rendered
    assert 'href="javascript:alert(1)"' not in rendered
    assert 'href="https://github.com/acme/repo/pull/42"' in rendered
    assert "prefers-color-scheme: light" in rendered
