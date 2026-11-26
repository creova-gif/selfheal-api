from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from selfheal_api.billing import BillingStore
from selfheal_api.github_app import GitHubAppClient, InstallationRecord, InstallationRegistry
from selfheal_api.ledger import DriftLedger
from selfheal_api.notifier import AlertNotifier, DriftAlert
from sentinel.detector import DriftReport, detect_drift
from sentinel.guard import compute_drift_fingerprint
from synthesizer.openapi_emitter import emit_openapi_schema_patch
from synthesizer.patcher import PatchResult, synthesize_patch, verify_patch
from synthesizer.zod_emitter import synthesize_zod_preprocessor


class RemediationRequest(BaseModel):
    """Input parameters for an automated schema-drift remediation run."""

    model_config = ConfigDict(frozen=True)

    org_id: str
    repo: str
    model_name: str
    model_source: str
    base_branch: str = "main"


class RemediationOutcome(BaseModel):
    """Structured result of an automated remediation pipeline execution."""

    model_config = ConfigDict(frozen=True)

    status: str
    fingerprint: str
    pr_url: str | None = None
    zod_adapter: str | None = None
    openapi_yaml: str | None = None
    detail: str = ""


def _split_owner_repo(repo: str, record: InstallationRecord) -> tuple[str, str]:
    if "/" in repo:
        owner, repo_name = repo.split("/", 1)
        return owner, repo_name
    owner = record.account_login or record.org_id
    return owner, repo


def _format_pr_body(
    req: RemediationRequest,
    fingerprint: str,
    report: DriftReport,
    patch_res: PatchResult,
    zod_adapter: str,
    openapi_yaml: str,
) -> str:
    kinds = ", ".join(str(issue.kind) for issue in report.issues) or "none"
    return (
        f"## Automated Schema Drift Remediation\n\n"
        f"- **Model:** `{req.model_name}`\n"
        f"- **Fingerprint:** `{fingerprint}`\n"
        f"- **Issues ({len(report.issues)}):** `{kinds}`\n\n"
        f"### Patched Pydantic Model\n\n"
        f"```python\n{patch_res.patched_source.rstrip()}\n```\n\n"
        f"### Zod Preprocessor\n\n"
        f"```typescript\n{zod_adapter.rstrip()}\n```\n\n"
        f"### OpenAPI 3.1.0 Patch\n\n"
        f"```yaml\n{openapi_yaml.rstrip()}\n```\n"
    )


class RemediationPipeline:
    """Orchestrates installation checks, quota checks, drift patching, verification, and PRs."""

    def __init__(
        self,
        billing_store: BillingStore,
        installation_registry: InstallationRegistry,
        github_client: GitHubAppClient,
        drift_ledger: DriftLedger,
        notifier: AlertNotifier | None = None,
    ) -> None:
        self.billing_store = billing_store
        self.installation_registry = installation_registry
        self.github_client = github_client
        self.drift_ledger = drift_ledger
        self.notifier = notifier

    def run(
        self,
        req: RemediationRequest,
        expected_model: type[BaseModel],
        drifted_payload: dict[str, Any],
        canonical_payload: dict[str, Any] | None = None,
    ) -> RemediationOutcome:
        """Execute the end-to-end remediation pipeline for a drifted payload."""
        record = self.installation_registry.get_by_org(req.org_id)
        if record is None:
            return RemediationOutcome(
                status="rejected_installation",
                fingerprint="",
                detail=f"No GitHub App installation found for org {req.org_id!r}",
            )
        if record.suspended:
            return RemediationOutcome(
                status="rejected_installation",
                fingerprint="",
                detail=f"GitHub App installation is suspended for org {req.org_id!r}",
            )
        if req.repo not in record.repositories:
            return RemediationOutcome(
                status="rejected_installation",
                fingerprint="",
                detail=f"Repository {req.repo!r} is not enabled for org {req.org_id!r}",
            )

        limits = self.billing_store.get_limits(req.org_id)
        summary = self.drift_ledger.get_org_summary(req.org_id)
        if summary.prs_opened >= limits.max_models:
            return RemediationOutcome(
                status="rejected_quota",
                fingerprint="",
                detail=(
                    f"Quota exceeded for org {req.org_id!r}: "
                    f"{summary.prs_opened}/{limits.max_models} PRs opened"
                ),
            )

        report = detect_drift(expected_model, drifted_payload)
        fingerprint = compute_drift_fingerprint(report)
        issue_kinds = tuple(str(issue.kind) for issue in report.issues)

        if not report.has_drift:
            self.drift_ledger.record_event(
                org_id=req.org_id,
                model_name=req.model_name,
                fingerprint=fingerprint,
                has_drift=False,
                issue_count=0,
                issue_kinds=(),
            )
            return RemediationOutcome(
                status="no_drift",
                fingerprint=fingerprint,
                detail=f"No schema drift detected for {req.model_name}",
            )

        patch_res = synthesize_patch(req.model_source, report, root_model=expected_model)
        verification = verify_patch(patch_res, drifted_payload, canonical_payload)
        if not verification.passed:
            self.drift_ledger.record_event(
                org_id=req.org_id,
                model_name=req.model_name,
                fingerprint=fingerprint,
                has_drift=True,
                issue_count=len(report.issues),
                issue_kinds=issue_kinds,
            )
            return RemediationOutcome(
                status="failed_verification",
                fingerprint=fingerprint,
                detail=verification.error_detail or "Patched model failed verification",
            )

        zod_adapter = synthesize_zod_preprocessor(patch_res.plan)
        openapi_spec = emit_openapi_schema_patch(expected_model, patch_res.plan)

        owner, repo_name = _split_owner_repo(req.repo, record)
        pr_title = f"fix(selfheal): remediate schema drift for {req.model_name}"
        pr_head = f"selfheal/{req.model_name.lower()}-{fingerprint[:12]}"
        pr_body = _format_pr_body(
            req=req,
            fingerprint=fingerprint,
            report=report,
            patch_res=patch_res,
            zod_adapter=zod_adapter,
            openapi_yaml=openapi_spec.yaml_snippet,
        )
        pr_payload = self.github_client.open_pull_request(
            owner=owner,
            repo=repo_name,
            title=pr_title,
            head=pr_head,
            base=req.base_branch,
            body=pr_body,
        )
        raw_url = pr_payload.get("html_url")
        pr_url = str(raw_url) if isinstance(raw_url, str) and raw_url else None

        self.drift_ledger.record_event(
            org_id=req.org_id,
            model_name=req.model_name,
            fingerprint=fingerprint,
            has_drift=True,
            issue_count=len(report.issues),
            issue_kinds=issue_kinds,
            pr_url=pr_url,
        )

        if self.notifier is not None:
            self.notifier.notify(
                DriftAlert(
                    org_id=req.org_id,
                    model_name=req.model_name,
                    fingerprint=fingerprint,
                    issue_count=len(report.issues),
                    issue_kinds=issue_kinds,
                    pr_url=pr_url,
                )
            )

        return RemediationOutcome(
            status="pr_opened",
            fingerprint=fingerprint,
            pr_url=pr_url,
            zod_adapter=zod_adapter,
            openapi_yaml=openapi_spec.yaml_snippet,
            detail=f"Opened remediation PR for {req.model_name}",
        )
