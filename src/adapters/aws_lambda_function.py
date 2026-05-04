"""AWS Lambda function cost adapter.

Lambda pricing is fundamentally usage-dependent (charged per request
and per GB-second of execution). A Terraform plan tells us nothing
about actual invocation count or duration, so this adapter returns
an *illustrative* monthly figure under fixed assumptions:

    1,000,000 invocations / month, 100 ms average duration

Real workloads will diverge — confidence is always NONE so this
number must not be treated as a hard gate. It exists to give an
order-of-magnitude reference in PR comments.

Pricing-relevant fields on `aws_lambda_function`:
  memory_size, architectures, region

Never raises.
"""
from __future__ import annotations

import logging
from typing import Any

from src.adapters._aws_common import (
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

logger = logging.getLogger("cost-gate.aws_lambda_function")

# Illustrative usage assumptions — see module docstring.
ASSUMED_INVOCATIONS_PER_MONTH = 1_000_000
ASSUMED_DURATION_SECONDS = 0.1
DEFAULT_MEMORY_MB = 128

ASSUMPTION_NOTE = (
    f"Illustrative — assumes {ASSUMED_INVOCATIONS_PER_MONTH:,} "
    f"invocations/mo at {int(ASSUMED_DURATION_SECONDS * 1000)}ms each. "
    f"Lambda cost is usage_dependent; treat this as order-of-magnitude only."
)


def _duration_group(architectures: list[str] | None) -> tuple[str, str]:
    """Pricing API group name and human label for the duration SKU."""
    if architectures and isinstance(architectures, list) and "arm64" in architectures:
        return "AWS-Lambda-Duration-ARM", "arm64"
    return "AWS-Lambda-Duration", "x86_64"


class AwsLambdaFunctionAdapter:
    """Cost adapter for the `aws_lambda_function` Terraform resource type."""

    resource_type = "aws_lambda_function"
    pricing_basis = PricingBasis.USAGE_DEPENDENT

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
                planned, p_notes = await self._lookup_monthly(after)
                return self._build(
                    address, action, region,
                    None, planned, [], p_notes,
                )

            case ChangeAction.DELETE:
                prior, b_notes = await self._lookup_monthly(before)
                return self._build(
                    address, action, region,
                    prior, None, b_notes, [],
                )

            case ChangeAction.REPLACE:
                prior, b_notes = await self._lookup_monthly(before)
                planned, p_notes = await self._lookup_monthly(after)
                return self._build(
                    address, action, region,
                    prior, planned, b_notes, p_notes,
                )

            case ChangeAction.UPDATE:
                if (
                    before.get("memory_size") == after.get("memory_size")
                    and before.get("architectures") == after.get("architectures")
                ):
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
                        notes=[
                            "No pricing-relevant fields changed "
                            "(memory_size, architectures)",
                            ASSUMPTION_NOTE,
                        ],
                    )
                prior, b_notes = await self._lookup_monthly(before)
                planned, p_notes = await self._lookup_monthly(after)
                return self._build(
                    address, action, region,
                    prior, planned, b_notes, p_notes,
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
        prior_monthly: float | None,
        planned_monthly: float | None,
        prior_notes: list[str],
        planned_notes: list[str],
    ) -> ResourceCostEstimate:
        seen: set[str] = set()
        notes: list[str] = []
        for n in (*prior_notes, *planned_notes, ASSUMPTION_NOTE):
            if n not in seen:
                seen.add(n)
                notes.append(n)

        prior_rounded = (
            round(prior_monthly, 4) if prior_monthly is not None else None
        )
        planned_rounded = (
            round(planned_monthly, 4) if planned_monthly is not None else None
        )

        match action:
            case ChangeAction.CREATE:
                delta = planned_rounded if planned_rounded is not None else 0.0
                lookup_failed = planned_rounded is None
            case ChangeAction.DELETE:
                delta = -prior_rounded if prior_rounded is not None else 0.0
                lookup_failed = prior_rounded is None
            case ChangeAction.REPLACE:
                # Replace = destroy-and-create. Net delta is planned − prior,
                # mirroring update; the prior cost lives in
                # monthly_cost_prior_usd, not in a free-text note.
                if planned_rounded is None or prior_rounded is None:
                    delta = 0.0
                    lookup_failed = True
                else:
                    delta = planned_rounded - prior_rounded
                    lookup_failed = False
            case ChangeAction.UPDATE:
                if planned_rounded is None or prior_rounded is None:
                    delta = 0.0
                    lookup_failed = True
                else:
                    delta = planned_rounded - prior_rounded
                    lookup_failed = False
            case _:
                delta = 0.0
                lookup_failed = True

        if lookup_failed:
            pricing_source = PricingSource.UNKNOWN
        else:
            pricing_source = PricingSource.FALLBACK_ESTIMATE

        # Lambda is always usage_dependent — confidence stays NONE
        # regardless of whether the rate lookup succeeded.
        return ResourceCostEstimate(
            address=address,
            resource_type=self.resource_type,
            pricing_basis=self.pricing_basis,
            cloud=CloudProvider.AWS,
            region=region,
            change_action=action,
            monthly_cost_usd=round(delta, 4),
            monthly_cost_prior_usd=prior_rounded,
            monthly_cost_planned_usd=planned_rounded,
            pricing_source=pricing_source,
            confidence=Confidence.NONE,
            notes=notes,
        )

    async def _lookup_monthly(
        self, values: dict[str, Any],
    ) -> tuple[float | None, list[str]]:
        """Compute the illustrative monthly figure under the fixed assumptions.

        Returns (monthly_usd, notes). Cost is None if neither rate
        could be fetched from the Pricing API.
        """
        notes: list[str] = []
        region = values.get("region")
        memory_mb = values.get("memory_size") or DEFAULT_MEMORY_MB
        arch_group, arch_label = _duration_group(values.get("architectures"))

        if not region:
            notes.append("Missing region")
            return None, notes
        location = region_to_location(region)
        if not location:
            notes.append(f"No location mapping for region {region}")
            return None, notes

        # Duration rate ($/GB-s)
        duration_filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": location},
            {"Type": "TERM_MATCH", "Field": "group", "Value": arch_group},
        ]
        items = await get_products("AWSLambda", duration_filters, max_results=1)
        gb_second_rate = first_ondemand_usd(items[0]) if items else None
        if gb_second_rate is None:
            notes.append(
                f"No Pricing API match for Lambda duration "
                f"({arch_label}, {region})"
            )
            return None, notes

        # Request rate ($/request)
        request_filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": location},
            {"Type": "TERM_MATCH", "Field": "group", "Value": "AWS-Lambda-Requests"},
        ]
        request_items = await get_products(
            "AWSLambda", request_filters, max_results=1,
        )
        request_rate = (
            first_ondemand_usd(request_items[0]) if request_items else None
        )
        if request_rate is None:
            notes.append(
                f"No Pricing API match for Lambda requests in {region}"
            )
            return None, notes

        try:
            memory_gb = float(memory_mb) / 1024.0
        except (TypeError, ValueError):
            notes.append(f"Could not parse memory_size={memory_mb!r}")
            return None, notes

        gb_seconds = (
            memory_gb * ASSUMED_DURATION_SECONDS * ASSUMED_INVOCATIONS_PER_MONTH
        )
        duration_cost = gb_seconds * gb_second_rate
        request_cost = ASSUMED_INVOCATIONS_PER_MONTH * request_rate
        notes.append(
            f"memory_size={int(memory_mb)}MB architecture={arch_label}"
        )
        return duration_cost + request_cost, notes
