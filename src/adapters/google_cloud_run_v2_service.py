"""Google Cloud Run v2 service cost adapter.

Cloud Run v2 is billed per-CPU-second, per-GB-second, and per-request
(when min_instance_count=0; otherwise vCPU/RAM are billed for the
always-on instances). Like S3 bucket creation, the existence of a
service has no fixed monthly charge — the cost lives entirely in
usage and isn't visible from a Terraform plan.

This adapter returns $0 with confidence=NONE and a single note so
the service appears in the PR comment without skewing the threshold.

Never raises.
"""
from __future__ import annotations

from typing import Any

from src.adapters._gcp_common import region_from_zone
from src.models import (
    ChangeAction,
    CloudProvider,
    Confidence,
    PricingBasis,
    PricingSource,
    ResourceCostEstimate,
)

USAGE_NOTE = (
    "Cloud Run pricing is usage_dependent (per-vCPU-second, "
    "per-GB-second, per-request). Service creation is free; cost "
    "depends on traffic and min_instance_count."
)


class GoogleCloudRunV2ServiceAdapter:
    """Cost adapter for the `google_cloud_run_v2_service` Terraform resource."""

    resource_type = "google_cloud_run_v2_service"
    pricing_basis = PricingBasis.USAGE_DEPENDENT

    async def estimate(self, change: dict[str, Any]) -> ResourceCostEstimate:
        address = change.get("address", "unknown")
        change_block = change.get("change") or {}
        actions = change_block.get("actions") or []
        before = change_block.get("before") or {}
        after = change_block.get("after") or {}

        action = self._collapse_action(actions)
        # Cloud Run v2 puts the region in `location`, occasionally a zone.
        region = (
            after.get("location")
            or before.get("location")
            or region_from_zone(after.get("zone"))
        )

        return ResourceCostEstimate(
            address=address,
            resource_type=self.resource_type,
            pricing_basis=self.pricing_basis,
            cloud=CloudProvider.GCP,
            region=region,
            change_action=action,
            monthly_cost_usd=0.0,
            monthly_cost_prior_usd=None,
            monthly_cost_planned_usd=None,
            pricing_source=PricingSource.FALLBACK_ESTIMATE,
            confidence=Confidence.NONE,
            notes=[USAGE_NOTE],
        )

    @staticmethod
    def _collapse_action(actions: list[str]) -> ChangeAction:
        if "create" in actions and "delete" in actions:
            return ChangeAction.REPLACE
        if not actions:
            return ChangeAction.NO_OP
        try:
            return ChangeAction(actions[0])
        except ValueError:
            return ChangeAction.NO_OP
