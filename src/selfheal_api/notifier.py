from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
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


def _validate_webhook_config(webhook_url: str, timeout_s: float) -> None:
    parsed = urllib.parse.urlparse(webhook_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("webhook_url must be a valid https:// URL")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be > 0")


def _post_json(
    *,
    webhook_url: str,
    body: bytes,
    headers: dict[str, str],
    timeout_s: float,
    alert: DriftAlert,
    channel_label: str,
) -> bool:
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            status = getattr(response, "status", 200)
            return 200 <= int(status) < 300
    except (urllib.error.URLError, OSError) as exc:
        logger.warning(
            "%s drift alert delivery failed for org=%s model=%s: %s",
            channel_label,
            alert.org_id,
            alert.model_name,
            type(exc).__name__,
        )
        return False


class UrllibSlackNotifier:
    """Slack webhook notifier using stdlib urllib with fail-closed error handling."""

    def __init__(self, webhook_url: str, *, timeout_s: float = 5.0) -> None:
        _validate_webhook_config(webhook_url, timeout_s)
        self._webhook_url = webhook_url
        self._timeout_s = timeout_s

    def notify(self, alert: DriftAlert) -> bool:
        payload = format_slack_drift_alert(alert)
        body = json.dumps(payload).encode("utf-8")
        return _post_json(
            webhook_url=self._webhook_url,
            body=body,
            headers={"Content-Type": "application/json"},
            timeout_s=self._timeout_s,
            alert=alert,
            channel_label="Slack",
        )


class SignedWebhookNotifier:
    """Generic JSON webhook notifier with optional HMAC-SHA256 request signing."""

    def __init__(
        self,
        webhook_url: str,
        *,
        signing_secret: str = "",
        timeout_s: float = 5.0,
    ) -> None:
        _validate_webhook_config(webhook_url, timeout_s)
        self._webhook_url = webhook_url
        self._signing_secret = signing_secret
        self._timeout_s = timeout_s

    def notify(self, alert: DriftAlert) -> bool:
        body = json.dumps(
            alert.model_dump(mode="json"),
            separators=(",", ":"),
        ).encode("utf-8")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._signing_secret:
            digest = hmac.new(
                self._signing_secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
            headers["X-SelfHeal-Signature"] = f"sha256={digest}"

        return _post_json(
            webhook_url=self._webhook_url,
            body=body,
            headers=headers,
            timeout_s=self._timeout_s,
            alert=alert,
            channel_label="Signed webhook",
        )


class CompositeAlertNotifier:
    """Fan-out notifier that delivers alerts across multiple independent channels."""

    def __init__(self, notifiers: Sequence[AlertNotifier]) -> None:
        self._notifiers = tuple(notifiers)

    def notify(self, alert: DriftAlert) -> bool:
        any_succeeded = False
        for notifier in self._notifiers:
            try:
                if notifier.notify(alert):
                    any_succeeded = True
            except Exception as exc:
                logger.warning(
                    "Alert notifier %s raised %s for org=%s model=%s",
                    type(notifier).__name__,
                    type(exc).__name__,
                    alert.org_id,
                    alert.model_name,
                )
        return any_succeeded
