from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

import pytest
from pydantic import ValidationError

from selfheal_api.notifier import (
    AlertNotifier,
    DriftAlert,
    MockAlertNotifier,
    UrllibSlackNotifier,
    format_slack_drift_alert,
)


def _sample_alert(*, pr_url: str | None = "https://github.com/acme/api/pull/7") -> DriftAlert:
    return DriftAlert(
        org_id="org-123",
        model_name="CheckoutSession",
        fingerprint="sha256:abcd1234",
        issue_count=2,
        issue_kinds=("missing_field", "type_mismatch"),
        pr_url=pr_url,
    )


def test_drift_alert_is_frozen() -> None:
    alert = _sample_alert()
    with pytest.raises(ValidationError):
        alert.model_name = "OtherModel"


def test_format_slack_drift_alert_contains_only_structural_metadata() -> None:
    alert = _sample_alert()
    payload = format_slack_drift_alert(alert)

    serialized = json.dumps(payload)
    assert "org-123" in serialized
    assert "CheckoutSession" in serialized
    assert "sha256:abcd1234" in serialized
    assert "missing_field, type_mismatch" in serialized
    assert "https://github.com/acme/api/pull/7" in serialized
    assert len(payload["blocks"]) == 3

    no_pr_payload = format_slack_drift_alert(_sample_alert(pr_url=None))
    assert len(no_pr_payload["blocks"]) == 2
    assert "Remediation PR" not in json.dumps(no_pr_payload)


def test_mock_alert_notifier() -> None:
    notifier: AlertNotifier = MockAlertNotifier()
    alert = _sample_alert()
    assert notifier.notify(alert) is True
    assert isinstance(notifier, MockAlertNotifier)
    assert notifier.alerts == [alert]

    failing = MockAlertNotifier(should_succeed=False)
    assert failing.notify(alert) is False
    assert failing.alerts == [alert]


def test_urllib_slack_notifier_validates_webhook_url() -> None:
    for invalid_url in ["", "http://hooks.slack.com/services/a/b", "ftp://example.com", "https://"]:
        with pytest.raises(ValueError, match="https://"):
            UrllibSlackNotifier(invalid_url)

    with pytest.raises(ValueError, match="timeout_s"):
        UrllibSlackNotifier("https://hooks.slack.com/services/T00/B00/XXX", timeout_s=0)


class _FakeHTTPResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_urllib_slack_notifier_posts_json_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(req: urllib.request.Request, timeout: float = 0.0) -> _FakeHTTPResponse:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data
        captured["timeout"] = timeout
        return _FakeHTTPResponse(status=200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    notifier = UrllibSlackNotifier("https://hooks.slack.com/services/T00/B00/SECRET")
    assert notifier.notify(_sample_alert()) is True
    assert captured["url"] == "https://hooks.slack.com/services/T00/B00/SECRET"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 5.0
    assert captured["headers"]["Content-type"] == "application/json"

    decoded = json.loads(captured["body"].decode("utf-8"))
    assert "CheckoutSession" in decoded["text"]


def test_urllib_slack_notifier_fails_closed_on_network_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def raise_url_error(req: urllib.request.Request, timeout: float = 0.0) -> _FakeHTTPResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", raise_url_error)

    notifier = UrllibSlackNotifier("https://hooks.slack.com/services/T00/B00/SECRET")
    with caplog.at_level(logging.WARNING):
        result = notifier.notify(_sample_alert())

    assert result is False
    assert "URLError" in caplog.text
    assert "SECRET" not in caplog.text

    def raise_os_error(req: urllib.request.Request, timeout: float = 0.0) -> _FakeHTTPResponse:
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", raise_os_error)
    assert notifier.notify(_sample_alert()) is False
