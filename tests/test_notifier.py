from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from typing import Any

import pytest
from pydantic import ValidationError

from selfheal_api.notifier import (
    AlertNotifier,
    CompositeAlertNotifier,
    DriftAlert,
    MockAlertNotifier,
    SignedWebhookNotifier,
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


def test_signed_webhook_notifier_validates_url_and_timeout() -> None:
    for invalid_url in ["", "http://example.com/hook", "ftp://example.com/hook", "https://"]:
        with pytest.raises(ValueError, match="https://"):
            SignedWebhookNotifier(invalid_url)

    with pytest.raises(ValueError, match="timeout_s"):
        SignedWebhookNotifier("https://example.com/hook", timeout_s=0)


def test_signed_webhook_notifier_signs_request_with_hmac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(req: urllib.request.Request, timeout: float = 0.0) -> _FakeHTTPResponse:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = req.data
        captured["timeout"] = timeout
        return _FakeHTTPResponse(status=202)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    secret = "whsec_super_secret_key"
    alert = _sample_alert()
    notifier = SignedWebhookNotifier(
        "https://hooks.example.com/selfheal",
        signing_secret=secret,
        timeout_s=3.5,
    )
    assert notifier.notify(alert) is True
    assert captured["url"] == "https://hooks.example.com/selfheal"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 3.5
    assert captured["headers"]["content-type"] == "application/json"

    body: bytes = captured["body"]
    expected_digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert captured["headers"]["x-selfheal-signature"] == f"sha256={expected_digest}"
    assert json.loads(body.decode("utf-8")) == alert.model_dump(mode="json")

    captured.clear()
    unsigned = SignedWebhookNotifier("https://hooks.example.com/selfheal")
    assert unsigned.notify(alert) is True
    assert "x-selfheal-signature" not in captured["headers"]


def test_signed_webhook_notifier_fails_closed_without_leaking_secrets(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "whsec_do_not_log_me"

    def raise_url_error(req: urllib.request.Request, timeout: float = 0.0) -> _FakeHTTPResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", raise_url_error)

    notifier = SignedWebhookNotifier(
        "https://hooks.example.com/SECRET_PATH",
        signing_secret=secret,
    )
    with caplog.at_level(logging.WARNING):
        result = notifier.notify(_sample_alert())

    assert result is False
    assert "URLError" in caplog.text
    assert secret not in caplog.text
    assert "SECRET_PATH" not in caplog.text

    def return_non_2xx(req: urllib.request.Request, timeout: float = 0.0) -> _FakeHTTPResponse:
        return _FakeHTTPResponse(status=503)

    monkeypatch.setattr(urllib.request, "urlopen", return_non_2xx)
    assert notifier.notify(_sample_alert()) is False


class _ExplodingNotifier:
    def __init__(self) -> None:
        self.calls = 0

    def notify(self, alert: DriftAlert) -> bool:
        self.calls += 1
        raise RuntimeError("boom_secret_detail")


def test_composite_alert_notifier_fanout_and_isolation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    alert = _sample_alert()
    failing = MockAlertNotifier(should_succeed=False)
    exploding = _ExplodingNotifier()
    succeeding = MockAlertNotifier(should_succeed=True)

    composite = CompositeAlertNotifier([failing, exploding, succeeding])
    with caplog.at_level(logging.WARNING):
        result = composite.notify(alert)

    assert result is True
    assert failing.alerts == [alert]
    assert exploding.calls == 1
    assert succeeding.alerts == [alert]
    assert "RuntimeError" in caplog.text
    assert "boom_secret_detail" not in caplog.text

    all_failing = CompositeAlertNotifier(
        [MockAlertNotifier(should_succeed=False), _ExplodingNotifier()]
    )
    assert all_failing.notify(alert) is False
    assert CompositeAlertNotifier([]).notify(alert) is False
