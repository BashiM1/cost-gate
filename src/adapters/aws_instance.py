"""AWS EC2 instance cost adapter.

Calls the AWS Pricing API (us-east-1 — only region this API is hosted in)
to look up the on-demand hourly rate for an EC2 instance type, then
projects monthly cost on a 730-hour month.

Never raises: every failure path returns confidence=NONE with a
human-readable note so the response shape stays consistent.
"""
from __future__ import annotations

import logging
from typing import Any

from src.adapters._aws_common import (
    HOURS_PER_MONTH,
    first_ondemand_usd,
    get_products,
    region_to_location,
)
from src.models import (
    ChangeAction,
    CloudProvider,
    Confidence,
    PricingBasis,
    PricingSource,
    ResourceCostEstimate,
)

logger = logging.getLogger("cost-gate.aws_instance")

# Terraform tenancy → Pricing API tenancy
_TENANCY_MAP: dict[str, str] = {
    "default": "Shared",
    "dedicated": "Dedicated",
    "host": "Host",
}


def _map_tenancy(tf_tenancy: str | None) -> str:
    if not tf_tenancy:
        return "Shared"
    return _TENANCY_MAP.get(tf_tenancy.lower(), "Shared")


def _pricing_filters(instance_type: str, location: str, tenancy: str) -> list[dict]:
    return [
        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
        {"Type": "TERM_MATCH", "Field": "location", "Value": location},
        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": tenancy},
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
    ]


class AwsInstanceAdapter:
    """Cost adapter for the `aws_instance` Terraform resource type."""

    resource_type = "aws_instance"
    pricing_basis = PricingBasis.CONFIG_DEPENDENT

    async def estimate(self, change: dict[str, Any]) -> ResourceCostEstimate:
        address = change.get("address", "unknown")
        change_block = change.get("change") or {}
        actions = change_block.get("actions") or []
        before = change_block.get("before") or {}
        after = change_block.get("after") or {}

        action = self._collapse_action(actions)
        region = after.get("region") or before.get("region")

        match action:
            case ChangeAction.CREATE:
                planned = await self._lookup(after)
                return self._build(
                    address, action, region,
                    prior_hourly=None, planned_hourly=planned,
                    after=after, before=before,
                )

            case ChangeAction.DELETE:
                prior = await self._lookup(before)
                return self._build(
                    address, action, region,
                    prior_hourly=prior, planned_hourly=None,
                    after=after, before=before,
                )

            case ChangeAction.REPLACE:
                prior, planned = (
                    await self._lookup(before),
                    await self._lookup(after),
                )
                return self._build(
                    address, action, region,
                    prior_hourly=prior, planned_hourly=planned,
                    after=after, before=before,
                )

            case ChangeAction.UPDATE:
                same_type = before.get("instance_type") == after.get("instance_type")
                same_tenancy = (
                    _map_tenancy(before.get("tenancy"))
                    == _map_tenancy(after.get("tenancy"))
                )
                if same_type and same_tenancy:
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
                        pricing_source=PricingSource.AWS_PRICING_API,
                        confidence=Confidence.HIGH,
                        notes=[
                            "No pricing-relevant fields changed "
                            "(instance_type, tenancy)"
                        ],
                    )
                prior, planned = (
                    await self._lookup(before),
                    await self._lookup(after),
                )
                return self._build(
                    address, action, region,
                    prior_hourly=prior, planned_hourly=planned,
                    after=after, before=before,
                )

            case _:
                return ResourceCostEstimate(
                    address=address,
                    resource_type=self.resource_type,
                    pricing_basis=self.pricing_basis,
                    cloud=CloudProvider.AWS,
                    region=region,
                    change_action=action,
                    monthly_cost_usd=0.0,
                    pricing_source=PricingSource.UNKNOWN,
                    confidence=Confidence.NONE,
                    notes=[f"Unhandled action: {action.value}"],
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

    def _build(
        self,
        address: str,
        action: ChangeAction,
        region: str | None,
        prior_hourly: float | None,
        planned_hourly: float | None,
        after: dict,
        before: dict,
    ) -> ResourceCostEstimate:
        notes: list[str] = []

        prior_monthly = (
            round(prior_hourly * HOURS_PER_MONTH, 4)
            if prior_hourly is not None else None
        )
        planned_monthly = (
            round(planned_hourly * HOURS_PER_MONTH, 4)
            if planned_hourly is not None else None
        )

        # Delta semantics
        match action:
            case ChangeAction.CREATE:
                delta = planned_monthly if planned_monthly is not None else 0.0
                lookup_failed = planned_monthly is None
            case ChangeAction.DELETE:
                delta = -prior_monthly if prior_monthly is not None else 0.0
                lookup_failed = prior_monthly is None
            case ChangeAction.REPLACE:
                # Replace = destroy-and-create. Net delta is planned − prior,
                # mirroring update; the prior cost lives in
                # monthly_cost_prior_usd, not in a free-text note.
                if planned_monthly is None or prior_monthly is None:
                    delta = 0.0
                    lookup_failed = True
                else:
                    delta = planned_monthly - prior_monthly
                    lookup_failed = False
            case ChangeAction.UPDATE:
                if planned_monthly is None or prior_monthly is None:
                    delta = 0.0
                    lookup_failed = True
                else:
                    delta = planned_monthly - prior_monthly
                    lookup_failed = False
            case _:
                delta = 0.0
                lookup_failed = True

        if lookup_failed:
            confidence = Confidence.NONE
            pricing_source = PricingSource.UNKNOWN
            notes.append(
                f"Pricing API lookup did not yield a rate "
                f"(instance_type before={before.get('instance_type')}, "
                f"after={after.get('instance_type')}, region={region})"
            )
        else:
            confidence = Confidence.HIGH
            pricing_source = PricingSource.AWS_PRICING_API

        # $0.00 guard. Some legitimate adapters will hit zero (e.g. a
        # spot quote of $0.00 or a free tier line item) — surface a
        # note for review. Re-evaluate per adapter; some resources
        # genuinely have near-zero pricing.
        zero_planned = planned_monthly is not None and planned_monthly == 0.0
        zero_prior = prior_monthly is not None and prior_monthly == 0.0
        if confidence == Confidence.HIGH and (zero_planned or zero_prior):
            notes.append(
                "Pricing API returned $0.00 — re-evaluate per adapter; "
                "some resources have near-zero pricing"
            )

        return ResourceCostEstimate(
            address=address,
            resource_type=self.resource_type,
            pricing_basis=self.pricing_basis,
            cloud=CloudProvider.AWS,
            region=region,
            change_action=action,
            monthly_cost_usd=round(delta, 4),
            monthly_cost_prior_usd=prior_monthly,
            monthly_cost_planned_usd=planned_monthly,
            pricing_source=pricing_source,
            confidence=confidence,
            notes=notes,
        )

    async def _lookup(self, values: dict[str, Any]) -> float | None:
        """Resolve an on-demand hourly rate. Returns None on any failure."""
        instance_type = values.get("instance_type")
        region = values.get("region")
        tenancy = _map_tenancy(values.get("tenancy"))

        if not instance_type or not region:
            logger.info(
                "Pricing skip: missing field instance_type=%s region=%s",
                instance_type, region,
            )
            return None

        location = region_to_location(region)
        if not location:
            logger.info("Pricing skip: no location mapping for region %s", region)
            return None

        items = await get_products(
            "AmazonEC2",
            _pricing_filters(instance_type, location, tenancy),
            max_results=1,
        )
        if not items:
            logger.info(
                "Pricing API returned 0 results for %s/%s/%s",
                instance_type, location, tenancy,
            )
            return None

        hourly = first_ondemand_usd(items[0])
        if hourly is None:
            logger.warning(
                "Could not extract hourly rate for %s/%s/%s",
                instance_type, location, tenancy,
            )
        return hourly
