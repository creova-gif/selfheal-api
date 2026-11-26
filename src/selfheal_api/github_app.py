from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.request
from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

_GITHUB_API_VERSION = "2022-11-28"


def verify_webhook_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """Verify HMAC-SHA256 webhook signature (`sha256=<hex>`)."""
    if not secret or not signature_header:
        return False
    header = signature_header.strip()
    if not header.lower().startswith("sha256="):
        return False
    provided_sig = f"sha256={header[7:].lower()}"
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected_sig = f"sha256={digest}"
    return hmac.compare_digest(expected_sig, provided_sig)


class InstallationRecord(BaseModel):
    """Immutable record of a GitHub App installation for an organization."""

    model_config = ConfigDict(frozen=True)

    installation_id: int
    org_id: str
    account_login: str
    repositories: tuple[str, ...] = ()
    suspended: bool = False


class InstallationRegistry:
    """Thread-safe registry mapping org_id and installation_id to installations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_org: dict[str, InstallationRecord] = {}
        self._by_installation: dict[int, str] = {}

    def upsert(self, record: InstallationRecord) -> InstallationRecord:
        """Insert or replace an installation record."""
        with self._lock:
            prev_org = self._by_installation.get(record.installation_id)
            if prev_org is not None and prev_org != record.org_id:
                self._by_org.pop(prev_org, None)

            prev_record = self._by_org.get(record.org_id)
            if prev_record is not None and prev_record.installation_id != record.installation_id:
                self._by_installation.pop(prev_record.installation_id, None)

            self._by_org[record.org_id] = record
            self._by_installation[record.installation_id] = record.org_id
            return record

    def list_org_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._by_org.keys())

    def remove(self, installation_id: int) -> InstallationRecord | None:
        """Remove an installation by installation_id, returning it if present."""
        with self._lock:
            org_id = self._by_installation.pop(installation_id, None)
            if org_id is None:
                return None
            return self._by_org.pop(org_id, None)

    def add_repositories(
        self, installation_id: int, repos: Iterable[str]
    ) -> InstallationRecord | None:
        """Add repositories to an existing installation, preserving order."""
        with self._lock:
            org_id = self._by_installation.get(installation_id)
            if org_id is None:
                return None
            record = self._by_org.get(org_id)
            if record is None:
                return None
            merged = tuple(dict.fromkeys([*record.repositories, *repos]))
            updated = record.model_copy(update={"repositories": merged})
            self._by_org[org_id] = updated
            return updated

    def remove_repositories(
        self, installation_id: int, repos: Iterable[str]
    ) -> InstallationRecord | None:
        """Remove repositories from an existing installation."""
        to_remove = set(repos)
        with self._lock:
            org_id = self._by_installation.get(installation_id)
            if org_id is None:
                return None
            record = self._by_org.get(org_id)
            if record is None:
                return None
            remaining = tuple(r for r in record.repositories if r not in to_remove)
            updated = record.model_copy(update={"repositories": remaining})
            self._by_org[org_id] = updated
            return updated

    def set_suspended(
        self, installation_id: int, suspended: bool
    ) -> InstallationRecord | None:
        """Update the suspended flag for an existing installation."""
        with self._lock:
            org_id = self._by_installation.get(installation_id)
            if org_id is None:
                return None
            record = self._by_org.get(org_id)
            if record is None:
                return None
            updated = record.model_copy(update={"suspended": suspended})
            self._by_org[org_id] = updated
            return updated

    def get_by_org(self, org_id: str) -> InstallationRecord | None:
        """Look up an installation record by org_id."""
        with self._lock:
            return self._by_org.get(org_id)

    def get_by_installation(self, installation_id: int) -> InstallationRecord | None:
        """Look up an installation record by installation_id."""
        with self._lock:
            org_id = self._by_installation.get(installation_id)
            if org_id is None:
                return None
            return self._by_org.get(org_id)


def _extract_repo_names(raw_repos: Any) -> tuple[str, ...]:
    if not isinstance(raw_repos, list):
        return ()
    names: list[str] = []
    for item in raw_repos:
        if isinstance(item, dict):
            name = item.get("full_name") or item.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        elif isinstance(item, str) and item:
            names.append(item)
    return tuple(dict.fromkeys(names))


def _is_valid_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def handle_github_webhook(
    secret: str,
    body: bytes,
    signature_header: str,
    event_type: str,
    registry: InstallationRegistry,
) -> tuple[int, dict[str, Any]]:
    """Verify and process a GitHub App webhook event."""
    if secret and not verify_webhook_signature(secret, body, signature_header):
        return 401, {"error": "Invalid webhook signature"}

    try:
        decoded = body.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"error": "Invalid JSON payload"}

    if not isinstance(payload, dict):
        return 400, {"error": "JSON payload must be an object"}

    if event_type == "ping":
        return 200, {
            "status": "ok",
            "event": "ping",
            "zen": str(payload.get("zen", "")),
        }

    if event_type == "installation":
        return _handle_installation_event(payload, registry)

    if event_type == "installation_repositories":
        return _handle_installation_repositories_event(payload, registry)

    return 200, {"status": "ignored", "event": event_type}


def _handle_installation_event(
    payload: dict[str, Any],
    registry: InstallationRegistry,
) -> tuple[int, dict[str, Any]]:
    action = payload.get("action")
    installation = payload.get("installation")
    if not isinstance(installation, dict):
        return 400, {"error": "Missing installation object"}

    installation_id = installation.get("id")
    if not _is_valid_int(installation_id):
        return 400, {"error": "Missing or invalid installation.id"}
    assert isinstance(installation_id, int)

    if action == "created":
        account = installation.get("account")
        account_login = ""
        if isinstance(account, dict) and isinstance(account.get("login"), str):
            account_login = account["login"]
        org_id = account_login or f"installation-{installation_id}"
        repos = _extract_repo_names(payload.get("repositories"))
        suspended = installation.get("suspended_at") is not None
        record = InstallationRecord(
            installation_id=installation_id,
            org_id=org_id,
            account_login=account_login,
            repositories=repos,
            suspended=suspended,
        )
        registry.upsert(record)
        return 200, {
            "status": "ok",
            "event": "installation",
            "action": "created",
            "installation_id": installation_id,
            "org_id": org_id,
        }

    if action == "deleted":
        removed = registry.remove(installation_id)
        return 200, {
            "status": "ok",
            "event": "installation",
            "action": "deleted",
            "installation_id": installation_id,
            "removed": removed is not None,
        }

    if action == "suspend":
        updated = registry.set_suspended(installation_id, True)
        return 200, {
            "status": "ok",
            "event": "installation",
            "action": "suspend",
            "installation_id": installation_id,
            "updated": updated is not None,
        }

    if action == "unsuspend":
        updated = registry.set_suspended(installation_id, False)
        return 200, {
            "status": "ok",
            "event": "installation",
            "action": "unsuspend",
            "installation_id": installation_id,
            "updated": updated is not None,
        }

    return 200, {"status": "ignored", "event": "installation", "action": action}


def _handle_installation_repositories_event(
    payload: dict[str, Any],
    registry: InstallationRegistry,
) -> tuple[int, dict[str, Any]]:
    action = payload.get("action")
    installation = payload.get("installation")
    if not isinstance(installation, dict):
        return 400, {"error": "Missing installation object"}

    installation_id = installation.get("id")
    if not _is_valid_int(installation_id):
        return 400, {"error": "Missing or invalid installation.id"}
    assert isinstance(installation_id, int)

    if action == "added":
        repos = _extract_repo_names(payload.get("repositories_added"))
        updated = registry.add_repositories(installation_id, repos)
        return 200, {
            "status": "ok",
            "event": "installation_repositories",
            "action": "added",
            "installation_id": installation_id,
            "repositories": list(updated.repositories) if updated is not None else [],
        }

    if action == "removed":
        repos = _extract_repo_names(payload.get("repositories_removed"))
        updated = registry.remove_repositories(installation_id, repos)
        return 200, {
            "status": "ok",
            "event": "installation_repositories",
            "action": "removed",
            "installation_id": installation_id,
            "repositories": list(updated.repositories) if updated is not None else [],
        }

    return 200, {
        "status": "ignored",
        "event": "installation_repositories",
        "action": action,
    }


@runtime_checkable
class GitHubAppClient(Protocol):
    """Protocol for opening pull requests as a GitHub App installation."""

    def open_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str = "",
    ) -> dict[str, Any]:
        """Open a pull request and return the GitHub PR payload."""
        ...


class MockGitHubAppClient:
    """In-memory GitHub App client recording opened PRs for deterministic tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.opened_prs: list[dict[str, Any]] = []

    def open_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            number = len(self.opened_prs) + 1
            pr: dict[str, Any] = {
                "number": number,
                "html_url": f"https://github.com/{owner}/{repo}/pull/{number}",
                "owner": owner,
                "repo": repo,
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "state": "open",
            }
            self.opened_prs.append(pr)
            return dict(pr)


def _validate_slug(value: str, field_name: str) -> None:
    if not value or "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"Invalid {field_name}: {value!r}")


class UrllibGitHubAppClient:
    """Stdlib urllib.request client using an installation access token."""

    def __init__(
        self,
        token: str,
        api_base_url: str = "https://api.github.com",
        timeout_seconds: float = 10.0,
    ) -> None:
        if not token:
            raise ValueError("token must be non-empty")
        self._token = token
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def open_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str = "",
    ) -> dict[str, Any]:
        _validate_slug(owner, "owner")
        _validate_slug(repo, "repo")
        url = f"{self._api_base_url}/repos/{owner}/{repo}/pulls"
        payload = json.dumps(
            {
                "title": title,
                "head": head,
                "base": base,
                "body": body,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                "User-Agent": "selfheal-api",
            },
        )
        with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
            raw = response.read().decode("utf-8")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("GitHub API response was not a JSON object")
        return parsed
