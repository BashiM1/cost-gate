"""Cost-gate FastAPI service.

Receives Terraform plan JSON from CI, runs each `resource_changes`
entry through the adapter registry (real Pricing API calls when an
adapter is registered, stubs otherwise), and returns a breakdown
ready to drop into a PR comment.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException

import src.adapters  # noqa: F401  — side-effect import wires the registry
from src.adapters import get_adapter, registered_types
from src.models import (
    ChangeAction,
    CloudProvider,
    Confidence,
    CostEstimateRequest,
    CostEstimateResponse,
    HealthResponse,
    PricingBasis,
    PricingSource,
    ResourceCostEstimate,
)
from src.slack import build_breach_message, post_to_slack

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("cost-gate")

app = FastAPI(
    title="Cost Gate",
    description="Multi-cloud cost-aware CI/CD gate for Terraform plans",
    version="0.2.0",
)

STUB_MONTHLY_COST_USD = 42.0


def _classify_cloud(resource_type: str) -> CloudProvider:
    if resource_type.startswith(("aws_", "awscc_")):
        return CloudProvider.AWS
    if resource_type.startswith(("google_", "google-beta_")):
        return CloudProvider.GCP
    if resource_type.startswith("azurerm_"):
        return CloudProvider.AZURE
    return CloudProvider.UNKNOWN


def _provider_key(resource_type: str) -> str | None:
    """Map a Terraform resource type to its provider_regions dict key.

    Returns the same string Terraform uses in `configuration.provider_config`
    (e.g. `aws`, `google`), not the CloudProvider enum value.
    """
    if resource_type.startswith(("aws_", "awscc_")):
        return "aws"
    if resource_type.startswith(("google_", "google-beta_")):
        return "google"
    if resource_type.startswith("azurerm_"):
        return "azurerm"
    return None


def _inject_region(change: dict, provider_regions: dict[str, str]) -> None:
    """Set a default region on `change.after` from provider_regions.

    AWS plan output never includes region in `change.after` (it's a
    provider-level setting). Some GCP resources only carry zone or
    location. We populate `region` (and `location` for GCP, since
    google_cloud_run_v2_service reads that key) without overwriting
    anything the plan already provided.
    """
    if not provider_regions:
        return
    key = _provider_key(change.get("type", ""))
    if key is None:
        return
    region = provider_regions.get(key)
    if not region:
        return
    change_block = change.setdefault("change", {})
    after = change_block.get("after")
    if not isinstance(after, dict):
        return
    after.setdefault("region", region)
    if key == "google":
        after.setdefault("location", region)


def _change_action(change: dict) -> ChangeAction:
    actions = (change.get("change") or {}).get("actions") or []
    if "create" in actions and "delete" in actions:
        return ChangeAction.REPLACE
    if not actions:
        return ChangeAction.NO_OP
    try:
        return ChangeAction(actions[0])
    except ValueError:
        return ChangeAction.NO_OP


async def _stub_one(change: dict, action: ChangeAction) -> ResourceCostEstimate:
    resource_type = change.get("type", "unknown")
    after = (change.get("change") or {}).get("after") or {}
    return ResourceCostEstimate(
        address=change.get("address", "unknown"),
        resource_type=resource_type,
        # Stubs cover unmapped resource types — the basis is genuinely
        # unknown. USAGE_DEPENDENT is the conservative pick: it tells
        # callers "don't gate on this number" the same way Lambda/S3 do.
        pricing_basis=PricingBasis.USAGE_DEPENDENT,
        cloud=_classify_cloud(resource_type),
        region=after.get("region") or after.get("location"),
        change_action=action,
        monthly_cost_usd=STUB_MONTHLY_COST_USD,
        monthly_cost_planned_usd=STUB_MONTHLY_COST_USD,
        pricing_source=PricingSource.STUB,
        confidence=Confidence.NONE,
        notes=[f"No adapter registered for {resource_type} — stubbed"],
    )


async def _estimate_changes(
    plan: dict,
    provider_regions: dict[str, str] | None = None,
) -> list[ResourceCostEstimate]:
    """Dispatch each plan change to its adapter (or a stub) in parallel."""
    changes = plan.get("resource_changes") or []
    regions = provider_regions or {}
    tasks: list = []

    for change in changes:
        action = _change_action(change)
        if action in (ChangeAction.NO_OP, ChangeAction.READ):
            continue

        _inject_region(change, regions)

        adapter = get_adapter(change.get("type", ""))
        if adapter is not None:
            tasks.append(adapter.estimate(change))
            continue

        # No adapter and a delete: we don't know the prior cost, so skip.
        if action == ChangeAction.DELETE:
            continue

        tasks.append(_stub_one(change, action))

    return list(await asyncio.gather(*tasks)) if tasks else []


def _build_summary(
    estimates: list[ResourceCostEstimate],
    total: float,
    threshold: float,
    breached: bool,
) -> str:
    verdict = "**BREACH**" if breached else "PASS"
    lines = [
        f"### Cost Gate — {verdict}",
        f"Net estimated monthly delta: **${total:,.2f}** "
        f"(threshold: ${threshold:,.2f})",
        "",
        "| Resource | Cloud | Action | Δ Monthly USD | Confidence |",
        "|---|---|---|---|---|",
    ]
    for e in estimates:
        lines.append(
            f"| `{e.address}` | {e.cloud.value} | {e.change_action.value} "
            f"| ${e.monthly_cost_usd:,.2f} | {e.confidence.value} |"
        )
    if not estimates:
        lines.append("| _no chargeable changes_ | — | — | $0.00 | — |")
    return "\n".join(lines)


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", version=app.version)


@app.get("/api/v1/adapters")
async def list_adapters() -> dict:
    return {"registered": registered_types()}


@app.post("/api/v1/notify-merge")
async def notify_merge(estimate: CostEstimateResponse) -> dict:
    """Forward a breach notification to the FinOps Slack channel.

    Called by the GitHub Action only after a PR merge AND a confirmed
    breach. The Action passes the same response body it already has from
    `/api/v1/cost-estimate`. The endpoint defensively re-checks
    `threshold_breached` so an accidental call cannot Slack-spam.
    """
    if not estimate.threshold_breached:
        raise HTTPException(
            status_code=400,
            detail="threshold_breached must be true; refusing to notify",
        )
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        raise HTTPException(
            status_code=503,
            detail="SLACK_WEBHOOK_URL not configured on the service",
        )
    blocks = build_breach_message(estimate)
    if not await post_to_slack(webhook, blocks):
        raise HTTPException(status_code=502, detail="Slack webhook POST failed")
    logger.info(
        "notify-merge sent: repo=%s pr=%s total=%.2f threshold=%.2f",
        estimate.pr_context.repository,
        estimate.pr_context.pr_number,
        estimate.total_monthly_cost_usd,
        estimate.threshold_usd_monthly,
    )
    return {"status": "ok"}


@app.post("/api/v1/cost-estimate", response_model=CostEstimateResponse)
async def cost_estimate(req: CostEstimateRequest) -> CostEstimateResponse:
    request_id = str(uuid.uuid4())
    logger.info(
        "cost_estimate request_id=%s repo=%s pr=%s",
        request_id,
        req.pr_context.repository,
        req.pr_context.pr_number,
    )

    estimates = await _estimate_changes(req.terraform_plan, req.provider_regions)
    total = round(sum(e.monthly_cost_usd for e in estimates), 2)
    breached = total > req.threshold_usd_monthly

    return CostEstimateResponse(
        request_id=request_id,
        pr_context=req.pr_context,
        estimates=estimates,
        total_monthly_cost_usd=total,
        threshold_usd_monthly=req.threshold_usd_monthly,
        threshold_breached=breached,
        summary_markdown=_build_summary(
            estimates, total, req.threshold_usd_monthly, breached,
        ),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
