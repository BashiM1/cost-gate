"""AWS NAT Gateway cost adapter.

NAT Gateway billing has two components:
  - Hourly fixed charge per gateway (region-dependent, ~$0.045-$0.062/hr).
    This is what we estimate.
  - Per-GB data processing charge (~$0.045-$0.062/GB). Usage_dependent
    and impossible to estimate from a Terraform plan, so it's surfaced
    as a note instead of a number.

Pricing API service code is `AmazonEC2`. The hourly SKU is identified
by groupDescription="Hourly charge for NAT Gateways".

Never raises.
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

logger = logging.getLogger("cost-gate.aws_nat_gateway")

DATA_PROCESSING_NOTE = (
    "Per-GB data processing charge is usage_dependent and not included "
    "in this estimate"
)


def _pricing_filters(location: str) -> list[dict]:
    return [
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "NAT Gateway"},
        {"Type": "TERM_MATCH", "Field": "location", "Value": location},
        {"Type": "TERM_MATCH", "Field": "groupDescription",
         "Value": "Hourly charge for NAT Gateways"},
    ]


class AwsNatGatewayAdapter:
    """Cost adapter for the `aws_nat_gateway` Terraform resource type."""

    resource_type = "aws_nat_gateway"
    pricing_basis = PricingBasis.FIXED

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
                )

            case ChangeAction.DELETE:
                prior = await self._lookup(before)
                return self._build(
                    address, action, region,
                    prior_hourly=prior, planned_hourly=None,
                )

            case ChangeAction.REPLACE:
                prior, planned = (
                    await self._lookup(before),
                    await self._lookup(after),
                )
                return self._build(
                    address, action, region,
                    prior_hourly=prior, planned_hourly=planned,
                )

            case ChangeAction.UPDATE:
                # Region is the only pricing-relevant field; an in-place
                # update that doesn't change region is a $0 delta.
                if before.get("region") == after.get("region"):
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
                            "No pricing-relevant fields changed (region)",
                            DATA_PROCESSING_NOTE,
                        ],
                    )
                prior, planned = (
                    await self._lookup(before),
                    await self._lookup(after),
                )
                return self._build(
                    address, action, region,
                    prior_hourly=prior, planned_hourly=planned,
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
                f"Pricing API lookup did not yield a NAT Gateway hourly "
                f"rate for region={region}"
            )
        else:
            confidence = Confidence.HIGH
            pricing_source = PricingSource.AWS_PRICING_API
            notes.append(DATA_PROCESSING_NOTE)

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
        """Resolve the hourly NAT Gateway rate. None on any failure."""
        region = values.get("region")
        if not region:
            logger.info("NAT GW pricing skip: missing region")
            return None

        location = region_to_location(region)
        if not location:
            logger.info("NAT GW pricing skip: no location mapping for %s", region)
            return None

        items = await get_products(
            "AmazonEC2", _pricing_filters(location), max_results=1,
        )
        if not items:
            logger.info(
                "NAT GW pricing API returned 0 results for region=%s", region
            )
            return None

        hourly = first_ondemand_usd(items[0])
        if hourly is None:
            logger.warning("Could not extract NAT GW hourly rate for %s", region)
        return hourly
