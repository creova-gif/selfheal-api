from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class DriftAlert(BaseModel):
    """Zero-PII structural alert payload for schema drift notifications."""

    model_config = ConfigDict(frozen=True)

    org_id: str
    model_name: str
    fingerprint: str
    issue_count: int
    issue_kinds: tuple[str, ...]
    pr_url: str | None = None


def format_slack_drift_alert(alert: DriftAlert) -> dict[str, Any]:
    """Build a Slack Block Kit payload containing strictly structural metadata."""
    kinds_text = ", ".join(alert.issue_kinds) if alert.issue_kinds else "none"
    summary_text = (
        f"Schema drift detected for {alert.model_name} "
        f"(org={alert.org_id}, issues={alert.issue_count}, fp={alert.fingerprint})"
    )

    fields: list[dict[str, str]] = [
        {"type": "mrkdwn", "text": f"*Org ID:*\n`{alert.org_id}`"},
        {"type": "mrkdwn", "text": f"*Model:*\n`{alert.model_name}`"},
        {"type": "mrkdwn", "text": f"*Fingerprint:*\n`{alert.fingerprint}`"},
        {"type": "mrkdwn", "text": f"*Issue Count:*\n{alert.issue_count}"},
        {"type": "mrkdwn", "text": f"*Issue Kinds:*\n`{kinds_text}`"},
    ]

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Schema Drift Detected: {alert.model_name}",
            },
        },
        {
            "type": "section",
            "fields": fields,
        },
    ]

    if alert.pr_url is not None:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Remediation PR:* <{alert.pr_url}|View Pull Request>",
                },
            }
        )

    return {
        "text": summary_text,
        "blocks": blocks,
    }


class AlertNotifier(Protocol):
    """Protocol for delivering zero-PII schema drift alerts."""

    def notify(self, alert: DriftAlert) -> bool: ...


class MockAlertNotifier:
    """In-memory thread-safe notifier for deterministic unit testing."""

    def __init__(self, *, should_succeed: bool = True) -> None:
        self._lock = threading.Lock()
        self._should_succeed = should_succeed
        self.alerts: list[DriftAlert] = []

    def notify(self, alert: DriftAlert) -> bool:
        with self._lock:
            self.alerts.append(alert)
            return self._should_succeed


class UrllibSlackNotifier:
    """Slack webhook notifier using stdlib urllib with fail-closed error handling."""

    def __init__(self, webhook_url: str, *, timeout_s: float = 5.0) -> None:
        parsed = urllib.parse.urlparse(webhook_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("webhook_url must be a valid https:// URL")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")

        self._webhook_url = webhook_url
        self._timeout_s = timeout_s

    def notify(self, alert: DriftAlert) -> bool:
        payload = format_slack_drift_alert(alert)
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                status = getattr(response, "status", 200)
                return 200 <= int(status) < 300
        except (urllib.error.URLError, OSError) as exc:
            logger.warning(
                "Slack drift alert delivery failed for org=%s model=%s: %s",
                alert.org_id,
                alert.model_name,
                type(exc).__name__,
            )
            return False
