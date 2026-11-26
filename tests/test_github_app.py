from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from selfheal_api.github_app import (
    GitHubAppClient,
    InstallationRecord,
    InstallationRegistry,
    MockGitHubAppClient,
    UrllibGitHubAppClient,
    handle_github_webhook,
)


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def test_webhook_signature_verification() -> None:
    registry = InstallationRegistry()
    secret = "super-secret"
    body = _encode({"zen": "Keep it logically awesome."})

    status_invalid, resp_invalid = handle_github_webhook(
        secret=secret,
        body=body,
        signature_header="sha256=deadbeef",
        event_type="ping",
        registry=registry,
    )
    assert status_invalid == 401
    assert "error" in resp_invalid

    status_missing, _ = handle_github_webhook(
        secret=secret,
        body=body,
        signature_header="",
        event_type="ping",
        registry=registry,
    )
    assert status_missing == 401

    status_ok, resp_ok = handle_github_webhook(
        secret=secret,
        body=body,
        signature_header=_sign(secret, body),
        event_type="ping",
        registry=registry,
    )
    assert status_ok == 200
    assert resp_ok["event"] == "ping"
    assert resp_ok["zen"] == "Keep it logically awesome."


def test_webhook_ping_without_secret() -> None:
    registry = InstallationRegistry()
    body = _encode({"zen": "Speak like a human."})

    status, resp = handle_github_webhook(
        secret="",
        body=body,
        signature_header="",
        event_type="ping",
        registry=registry,
    )
    assert status == 401
    assert "error" in resp


def test_installation_lifecycle_created_suspend_unsuspend_deleted() -> None:
    registry = InstallationRegistry()
    secret = "whsec_test"

    created_body = _encode(
        {
            "action": "created",
            "installation": {
                "id": 4242,
                "account": {"login": "acme-corp"},
                "suspended_at": None,
            },
            "repositories": [
                {"full_name": "acme-corp/payments-api"},
                {"full_name": "acme-corp/orders-api"},
            ],
        }
    )
    status, resp = handle_github_webhook(
        secret=secret,
        body=created_body,
        signature_header=_sign(secret, created_body),
        event_type="installation",
        registry=registry,
    )
    assert status == 200
    assert resp["action"] == "created"

    record = registry.get_by_org("acme-corp")
    assert record == InstallationRecord(
        installation_id=4242,
        org_id="acme-corp",
        account_login="acme-corp",
        repositories=("acme-corp/payments-api", "acme-corp/orders-api"),
        suspended=False,
    )
    assert registry.get_by_installation(4242) == record

    suspend_body = _encode(
        {
            "action": "suspend",
            "installation": {"id": 4242, "account": {"login": "acme-corp"}},
        }
    )
    status_s, _ = handle_github_webhook(
        secret=secret,
        body=suspend_body,
        signature_header=_sign(secret, suspend_body),
        event_type="installation",
        registry=registry,
    )
    assert status_s == 200
    suspended_record = registry.get_by_installation(4242)
    assert suspended_record is not None
    assert suspended_record.suspended is True

    unsuspend_body = _encode(
        {
            "action": "unsuspend",
            "installation": {"id": 4242, "account": {"login": "acme-corp"}},
        }
    )
    status_u, _ = handle_github_webhook(
        secret=secret,
        body=unsuspend_body,
        signature_header=_sign(secret, unsuspend_body),
        event_type="installation",
        registry=registry,
    )
    assert status_u == 200
    unsuspended_record = registry.get_by_installation(4242)
    assert unsuspended_record is not None
    assert unsuspended_record.suspended is False

    deleted_body = _encode(
        {
            "action": "deleted",
            "installation": {"id": 4242, "account": {"login": "acme-corp"}},
        }
    )
    status_d, resp_d = handle_github_webhook(
        secret=secret,
        body=deleted_body,
        signature_header=_sign(secret, deleted_body),
        event_type="installation",
        registry=registry,
    )
    assert status_d == 200
    assert resp_d["removed"] is True
    assert registry.get_by_org("acme-corp") is None
    assert registry.get_by_installation(4242) is None


def test_installation_repositories_added_and_removed() -> None:
    registry = InstallationRegistry()
    registry.upsert(
        InstallationRecord(
            installation_id=101,
            org_id="beta-org",
            account_login="beta-org",
            repositories=("beta-org/core",),
        )
    )
    secret = "repo-secret"

    added_body = _encode(
        {
            "action": "added",
            "installation": {"id": 101},
            "repositories_added": [
                {"full_name": "beta-org/core"},
                {"full_name": "beta-org/billing"},
                {"full_name": "beta-org/web"},
            ],
        }
    )
    status_add, resp_add = handle_github_webhook(
        secret=secret,
        body=added_body,
        signature_header=_sign(secret, added_body),
        event_type="installation_repositories",
        registry=registry,
    )
    assert status_add == 200
    assert resp_add["repositories"] == [
        "beta-org/core",
        "beta-org/billing",
        "beta-org/web",
    ]

    removed_body = _encode(
        {
            "action": "removed",
            "installation": {"id": 101},
            "repositories_removed": [{"full_name": "beta-org/billing"}],
        }
    )
    status_rem, resp_rem = handle_github_webhook(
        secret=secret,
        body=removed_body,
        signature_header=_sign(secret, removed_body),
        event_type="installation_repositories",
        registry=registry,
    )
    assert status_rem == 200
    assert resp_rem["repositories"] == ["beta-org/core", "beta-org/web"]
    record = registry.get_by_installation(101)
    assert record is not None
    assert record.repositories == ("beta-org/core", "beta-org/web")


def test_mock_github_app_client_records_opened_prs() -> None:
    client = MockGitHubAppClient()
    assert isinstance(client, GitHubAppClient)

    pr1 = client.open_pull_request(
        owner="acme-corp",
        repo="payments-api",
        title="fix: adapt schema drift for Invoice",
        head="selfheal/invoice-drift-1",
        base="main",
        body="Automated schema-drift patch.",
    )
    pr2 = client.open_pull_request(
        owner="acme-corp",
        repo="orders-api",
        title="fix: adapt schema drift for Order",
        head="selfheal/order-drift-2",
        base="main",
    )

    assert pr1["number"] == 1
    assert pr1["html_url"] == "https://github.com/acme-corp/payments-api/pull/1"
    assert pr2["number"] == 2
    assert len(client.opened_prs) == 2
    assert client.opened_prs[0]["title"] == "fix: adapt schema drift for Invoice"
    assert client.opened_prs[1]["body"] == ""


def test_urllib_github_app_client_protocol_and_validation() -> None:
    client = UrllibGitHubAppClient(token="ghs_test_token")
    assert isinstance(client, GitHubAppClient)

    with pytest.raises(ValueError):
        UrllibGitHubAppClient(token="")

    with pytest.raises(ValueError):
        client.open_pull_request(
            owner="../evil",
            repo="payments-api",
            title="t",
            head="h",
            base="main",
        )


def test_urllib_github_app_client_https_enforcement_and_slug_validation() -> None:
    with pytest.raises(ValueError, match="https://"):
        UrllibGitHubAppClient(token="ghs_test", api_base_url="http://api.github.com")

    with pytest.raises(ValueError, match="http://"):
        UrllibGitHubAppClient(
            token="ghs_test",
            api_base_url="ftp://api.github.com",
            allow_insecure_http=True,
        )

    insecure_client = UrllibGitHubAppClient(
        token="ghs_test",
        api_base_url="http://localhost:8080",
        allow_insecure_http=True,
    )
    assert isinstance(insecure_client, GitHubAppClient)

    for bad_slug in ("", "..", "org/sub", "org\\sub", "bad slug", "org?x=1", "a..b"):
        with pytest.raises(ValueError, match="Invalid owner"):
            insecure_client.open_pull_request(
                owner=bad_slug,
                repo="payments-api",
                title="t",
                head="h",
                base="main",
            )
        with pytest.raises(ValueError, match="Invalid repo"):
            insecure_client.open_pull_request(
                owner="acme-corp",
                repo=bad_slug,
                title="t",
                head="h",
                base="main",
            )
