"""Google Cloud SQL database instance cost adapter.

Cloud SQL pricing for `db-custom-*` tiers splits into vCPU/hour and
RAM/hour SKUs. `availability_type` selects between Zonal (single-zone)
and Regional (HA) SKU prefixes — the latter is roughly 2× the price.

Pricing-relevant fields:
  tier (db-custom-N-M form supported), database_version,
  availability_type, region

Storage cost (`disk_size_gb` × `disk_type`) is intentionally excluded —
it adds another two SKU lookups per call without changing the order
of magnitude. Notes call this out so users know it's missing.

Engines other than MySQL and PostgreSQL return confidence=NONE.

Never raises.
"""
from __future__ import annotations

import logging
from typing import Any

from src.adapters._gcp_common import (
    HOURS_PER_MONTH,
    SERVICE_ID_CLOUD_SQL,
    find_sku,
    first_ondemand_usd,
    list_skus,
)
from src.models import (
    ChangeAction,
    CloudProvider,
    Confidence,
    PricingBasis,
    PricingSource,
    ResourceCostEstimate,
)

logger = logging.getLogger("cost-gate.google_sql_database_instance")

_PRICING_FIELDS = (
    "tier", "database_version", "availability_type", "region",
)

# Catalog descriptions for newer / specialised SKUs that should not
# match a plain "vCPU" or "RAM" lookup. find_sku() applies these as
# extra excludes on top of the default Spot/Preemptible/Commitment.
_SQL_SKU_EXCLUDES = (
    "Spot", "Preemptible", "Commitment",
    "Enterprise Plus", "N4", "Extended", "FDC", "Trial", "Read Replica",
)


def _engine_from_version(dbv: str | None) -> str | None:
    """Map database_version (e.g. POSTGRES_15) to catalog fragment."""
    if not dbv:
        return None
    v = dbv.upper()
    if v.startswith("POSTGRES"):
        return "PostgreSQL"
    if v.startswith("MYSQL"):
        return "MySQL"
    return None


def _parse_custom_tier(tier: str | None) -> tuple[float, float] | None:
    """Parse `db-custom-N-M` → (vCPU, RAM_GB). Returns None on anything else."""
    if not tier:
        return None
    parts = tier.lower().split("-")
    if len(parts) != 4 or parts[0] != "db" or parts[1] != "custom":
        return None
    try:
        vcpu = float(parts[2])
        ram_mb = float(parts[3])
    except ValueError:
        return None
    return vcpu, ram_mb / 1024.0


def _deployment(availability_type: str | None) -> str:
    if availability_type and availability_type.upper() == "REGIONAL":
        return "Regional"
    return "Zonal"


class GoogleSqlDatabaseInstanceAdapter:
    """Cost adapter for the `google_sql_database_instance` Terraform resource."""

    resource_type = "google_sql_database_instance"
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
                        cloud=CloudProvider.GCP,
                        region=region,
                        change_action=action,
                        monthly_cost_usd=0.0,
                        monthly_cost_prior_usd=None,
                        monthly_cost_planned_usd=None,
                        pricing_source=PricingSource.GCP_BILLING_CATALOG,
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
                    cloud=CloudProvider.GCP,
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
            pricing_source = PricingSource.GCP_BILLING_CATALOG

        return ResourceCostEstimate(
            address=address,
            resource_type=self.resource_type,
            pricing_basis=self.pricing_basis,
            cloud=CloudProvider.GCP,
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
        notes: list[str] = []
        tier = values.get("tier")
        dbv = values.get("database_version")
        region = values.get("region")
        deployment = _deployment(values.get("availability_type"))

        if not region:
            notes.append("Missing region")
            return None, notes

        engine = _engine_from_version(dbv)
        if engine is None:
            notes.append(
                f"database_version={dbv!r} not supported "
                f"(adapter covers MySQL and PostgreSQL only)"
            )
            return None, notes

        parsed = _parse_custom_tier(tier)
        if parsed is None:
            notes.append(
                f"tier={tier!r} not in db-custom-N-M form "
                f"(other shapes not yet supported)"
            )
            return None, notes
        vcpu, ram_gb = parsed

        skus = await list_skus(SERVICE_ID_CLOUD_SQL)
        if not skus:
            notes.append(
                "GCP Cloud Billing Catalog SKU list is empty — "
                "auth/network failure (see container logs)"
            )
            return None, notes

        cpu_sku = find_sku(
            skus, region,
            (f"Cloud SQL for {engine}: {deployment} - vCPU",),
            description_excludes=_SQL_SKU_EXCLUDES,
        )
        ram_sku = find_sku(
            skus, region,
            (f"Cloud SQL for {engine}: {deployment} - RAM",),
            description_excludes=_SQL_SKU_EXCLUDES,
        )
        if cpu_sku is None or ram_sku is None:
            notes.append(
                f"No SKU match for Cloud SQL {engine} {deployment} "
                f"vCPU/RAM in {region}"
            )
            return None, notes

        cpu_per_hour = first_ondemand_usd(cpu_sku)
        ram_per_hour = first_ondemand_usd(ram_sku)
        if cpu_per_hour is None or ram_per_hour is None:
            notes.append(
                f"Could not extract vCPU/RAM rates for {engine} "
                f"{deployment} in {region}"
            )
            return None, notes

        monthly = (
            (vcpu * cpu_per_hour + ram_gb * ram_per_hour) * HOURS_PER_MONTH
        )
        notes.append(
            f"tier={tier} ({vcpu:g} vCPU, {ram_gb:g} GB RAM), "
            f"engine={engine}, {deployment}"
        )
        notes.append("Storage cost not included — disk pricing excluded")
        return monthly, notes
