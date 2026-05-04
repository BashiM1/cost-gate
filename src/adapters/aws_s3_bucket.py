"""AWS S3 bucket cost adapter.

Bucket creation itself is free — there's no fixed monthly charge for
the existence of a bucket. All meaningful S3 cost is usage-driven:
storage volume, request count, and data transfer. None of that is
visible in a Terraform plan.

This adapter returns $0 with confidence=NONE and a single note so
the bucket appears in the PR comment but doesn't influence the
threshold check.

Never raises.
"""
from __future__ import annotations

from typing import Any

from src.models import (
    ChangeAction,
    CloudProvider,
    Confidence,
    PricingBasis,
    PricingSource,
    ResourceCostEstimate,
)

USAGE_NOTE = (
    "S3 pricing depends on storage volume, request count, and data "
    "transfer. Bucket creation is free."
)


class AwsS3BucketAdapter:
    """Cost adapter for the `aws_s3_bucket` Terraform resource type."""

    resource_type = "aws_s3_bucket"
    pricing_basis = PricingBasis.USAGE_DEPENDENT

    async def estimate(self, change: dict[str, Any]) -> ResourceCostEstimate:
        address = change.get("address", "unknown")
        change_block = change.get("change") or {}
        actions = change_block.get("actions") or []
        before = change_block.get("before") or {}
        after = change_block.get("after") or {}

        action = self._collapse_action(actions)
        region = (
            after.get("region")
            or before.get("region")
            or after.get("bucket_region")
        )

        return ResourceCostEstimate(
            address=address,
            resource_type=self.resource_type,
            pricing_basis=self.pricing_basis,
            cloud=CloudProvider.AWS,
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
