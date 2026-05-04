"""AWS RDS DB Instance cost adapter.

Pricing has two lines:
  - Instance hourly  (productFamily=Database Instance)
  - Storage GB-month (productFamily=Database Storage)

Pricing-relevant fields on `aws_db_instance`:
  instance_class, engine, multi_az, allocated_storage, storage_type

Only the open-source engines (MySQL, PostgreSQL, MariaDB) are
implemented — Oracle and SQL Server require additional licenseModel
and databaseEdition filters and return confidence=NONE with a note.

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

logger = logging.getLogger("cost-gate.aws_db_instance")

# Map of supported Terraform engine prefixes → Pricing API databaseEngine
_OPEN_SOURCE_ENGINES: dict[str, str] = {
    "mysql": "MySQL",
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
    "mariadb": "MariaDB",
}

_STORAGE_TYPE_MAP: dict[str, str] = {
    "gp2": "General Purpose",
    "gp3": "General Purpose-GP3",
    "io1": "Provisioned IOPS",
    "io2": "Provisioned IOPS",
    "standard": "Magnetic",
}

# Fields whose value affects the price output. Update path checks
# whether any of these changed before calling the Pricing API.
_PRICING_FIELDS = (
    "instance_class", "engine", "multi_az",
    "allocated_storage", "storage_type",
)


def _normalize_engine(tf_engine: str | None) -> str | None:
    if not tf_engine:
        return None
    eng = tf_engine.lower()
    # postgresql must be checked before postgres so the longer prefix wins
    for prefix in ("postgresql", "postgres", "mysql", "mariadb"):
        if eng.startswith(prefix):
            return _OPEN_SOURCE_ENGINES[prefix]
    return None


def _normalize_storage_type(tf_storage_type: str | None) -> str:
    """RDS default since 2022 is gp3; treat unset as gp3."""
    if not tf_storage_type:
        return "General Purpose-GP3"
    return _STORAGE_TYPE_MAP.get(
        tf_storage_type.lower(), "General Purpose-GP3",
    )


class AwsDbInstanceAdapter:
    """Cost adapter for the `aws_db_instance` Terraform resource type."""

    resource_type = "aws_db_instance"
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
                if all(
                    before.get(f) == after.get(f) for f in _PRICING_FIELDS
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
                        pricing_source=PricingSource.AWS_PRICING_API,
                        confidence=Confidence.HIGH,
                        notes=[
                            "No pricing-relevant fields changed "
                            f"({', '.join(_PRICING_FIELDS)})"
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
        # Combine prior and planned notes, preserving order, deduped.
        seen: set[str] = set()
        notes: list[str] = []
        for n in (*prior_notes, *planned_notes):
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
            confidence = Confidence.NONE
            pricing_source = PricingSource.UNKNOWN
        else:
            confidence = Confidence.HIGH
            pricing_source = PricingSource.AWS_PRICING_API

        zero_planned = planned_rounded is not None and planned_rounded == 0.0
        zero_prior = prior_rounded is not None and prior_rounded == 0.0
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
            monthly_cost_prior_usd=prior_rounded,
            monthly_cost_planned_usd=planned_rounded,
            pricing_source=pricing_source,
            confidence=confidence,
            notes=notes,
        )

    async def _lookup_monthly(
        self, values: dict[str, Any],
    ) -> tuple[float | None, list[str]]:
        """Resolve a full RDS monthly cost (instance + storage).

        Returns (monthly_cost, notes). Cost is None on hard failure
        (instance lookup fails or required fields missing). Storage
        sub-failure leaves the instance figure intact and adds a note.
        """
        notes: list[str] = []
        instance_class = values.get("instance_class")
        tf_engine = values.get("engine")
        engine = _normalize_engine(tf_engine)
        multi_az = bool(values.get("multi_az"))
        allocated_storage = values.get("allocated_storage")
        storage_type_pricing = _normalize_storage_type(values.get("storage_type"))
        region = values.get("region")

        if not instance_class:
            notes.append("Missing instance_class")
            return None, notes
        if not region:
            notes.append("Missing region")
            return None, notes
        if not engine:
            notes.append(
                f"Engine {tf_engine!r} not yet supported "
                f"(adapter covers MySQL, PostgreSQL, MariaDB)"
            )
            return None, notes

        location = region_to_location(region)
        if not location:
            notes.append(f"No location mapping for region {region}")
            return None, notes

        deployment = "Multi-AZ" if multi_az else "Single-AZ"

        instance_filters = [
            {"Type": "TERM_MATCH", "Field": "productFamily",
             "Value": "Database Instance"},
            {"Type": "TERM_MATCH", "Field": "location", "Value": location},
            {"Type": "TERM_MATCH", "Field": "instanceType",
             "Value": instance_class},
            {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": engine},
            {"Type": "TERM_MATCH", "Field": "deploymentOption",
             "Value": deployment},
            {"Type": "TERM_MATCH", "Field": "licenseModel",
             "Value": "No license required"},
        ]
        items = await get_products(
            "AmazonRDS", instance_filters, max_results=1,
        )
        if not items:
            notes.append(
                f"No Pricing API match for "
                f"{instance_class}/{engine}/{deployment}"
            )
            return None, notes
        hourly = first_ondemand_usd(items[0])
        if hourly is None:
            notes.append(
                f"Could not extract hourly rate for {instance_class}"
            )
            return None, notes
        instance_monthly = hourly * HOURS_PER_MONTH

        # Storage component — failure here is non-fatal, downgrades to
        # an instance-only estimate with a note.
        storage_monthly = 0.0
        if not allocated_storage:
            notes.append(
                "allocated_storage not specified — storage cost excluded"
            )
        else:
            try:
                gb = float(allocated_storage)
            except (TypeError, ValueError):
                gb = 0.0
                notes.append(
                    f"Could not parse allocated_storage={allocated_storage!r}"
                )

            if gb > 0:
                storage_filters = [
                    {"Type": "TERM_MATCH", "Field": "productFamily",
                     "Value": "Database Storage"},
                    {"Type": "TERM_MATCH", "Field": "location",
                     "Value": location},
                    {"Type": "TERM_MATCH", "Field": "volumeType",
                     "Value": storage_type_pricing},
                    {"Type": "TERM_MATCH", "Field": "deploymentOption",
                     "Value": deployment},
                ]
                storage_items = await get_products(
                    "AmazonRDS", storage_filters, max_results=1,
                )
                if not storage_items:
                    notes.append(
                        f"No storage Pricing API match for "
                        f"{storage_type_pricing}/{deployment} — "
                        f"storage cost excluded"
                    )
                else:
                    per_gb = first_ondemand_usd(storage_items[0])
                    if per_gb is None:
                        notes.append(
                            f"Could not extract storage rate for "
                            f"{storage_type_pricing}"
                        )
                    else:
                        storage_monthly = per_gb * gb

        return instance_monthly + storage_monthly, notes
