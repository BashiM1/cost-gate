"""Google Compute Engine instance cost adapter.

GCE pricing breaks an instance into separate vCPU and RAM SKUs (per
hour). For shared-core machine types (e2-micro, e2-small, e2-medium,
f1-micro, g1-small) GCP publishes a single bundle SKU instead.

Pricing-relevant fields on `google_compute_instance`:
  machine_type, zone

Region is derived from zone (`europe-west2-a` → `europe-west2`).

Never raises.
"""
from __future__ import annotations

import logging
from typing import Any

from src.adapters._gcp_common import (
    HOURS_PER_MONTH,
    SERVICE_ID_COMPUTE_ENGINE,
    find_sku,
    first_ondemand_usd,
    list_skus,
    region_from_zone,
)
from src.models import (
    ChangeAction,
    CloudProvider,
    Confidence,
    PricingBasis,
    PricingSource,
    ResourceCostEstimate,
)

logger = logging.getLogger("cost-gate.google_compute_instance")

# Shared-core machine types: (vCPU equivalent, RAM GB). Their pricing
# comes from a single bundled SKU keyed by description fragment.
_SHARED_CORE: dict[str, tuple[float, float, str]] = {
    # name: (vcpu_for_display, ram_for_display, description_fragment)
    "e2-micro":  (0.25, 1.0,  "E2 Instance Core"),  # bundle uses Core/Ram split
    "e2-small":  (0.5,  2.0,  "E2 Instance Core"),
    "e2-medium": (1.0,  4.0,  "E2 Instance Core"),
}

# Non-shared-core series: machine_type prefix → resource_group fragment used
# in SKU descriptions. Add more series here as needed.
_SERIES_FRAGMENT: dict[str, str] = {
    "e2":  "E2",
    "n1":  "N1",
    "n2":  "N2",
    "n2d": "N2D",
    "c2":  "C2",
    "c2d": "C2D",
    "t2d": "T2D",
    "t2a": "T2A",
}

# vCPU → RAM GB ratio per "shape" suffix. Real GCP ratios are series-
# dependent; these are the typical numbers and good enough for an
# order-of-magnitude estimate.
_RATIO_RAM_PER_VCPU: dict[str, float] = {
    "standard": 4.0,
    "highmem":  8.0,
    "highcpu":  1.0,
}


def _parse_machine_type(
    mt: str | None,
) -> tuple[str, float, float, bool] | None:
    """Returns (series_fragment, vcpu, ram_gb, is_shared_core), or None."""
    if not mt:
        return None
    mt = mt.lower()

    if mt in _SHARED_CORE:
        vcpu, ram, frag = _SHARED_CORE[mt]
        return frag.split()[0], vcpu, ram, True

    parts = mt.split("-")
    if len(parts) < 3:
        return None
    series, shape = parts[0], parts[1]
    try:
        vcpu = float(parts[2])
    except ValueError:
        return None
    series_frag = _SERIES_FRAGMENT.get(series)
    ratio = _RATIO_RAM_PER_VCPU.get(shape)
    if not series_frag or not ratio:
        return None
    return series_frag, vcpu, vcpu * ratio, False


class GoogleComputeInstanceAdapter:
    """Cost adapter for the `google_compute_instance` Terraform resource."""

    resource_type = "google_compute_instance"
    pricing_basis = PricingBasis.CONFIG_DEPENDENT

    async def estimate(self, change: dict[str, Any]) -> ResourceCostEstimate:
        address = change.get("address", "unknown")
        change_block = change.get("change") or {}
        actions = change_block.get("actions") or []
        before = change_block.get("before") or {}
        after = change_block.get("after") or {}

        action = self._collapse_action(actions)
        region = region_from_zone(after.get("zone")) or region_from_zone(
            before.get("zone")
        )

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
                    before.get("machine_type") == after.get("machine_type")
                    and before.get("zone") == after.get("zone")
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
                            "(machine_type, zone)"
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

        zero_planned = planned_rounded is not None and planned_rounded == 0.0
        zero_prior = prior_rounded is not None and prior_rounded == 0.0
        if confidence == Confidence.HIGH and (zero_planned or zero_prior):
            notes.append(
                "Catalog returned $0.00 — re-evaluate per adapter; "
                "some resources have near-zero pricing"
            )

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
        machine_type = values.get("machine_type")
        zone = values.get("zone")
        region = region_from_zone(zone) or values.get("region")

        if not region:
            notes.append(f"Could not derive region (zone={zone!r})")
            return None, notes

        parsed = _parse_machine_type(machine_type)
        if not parsed:
            notes.append(
                f"machine_type={machine_type!r} not recognised "
                f"(supported series: {', '.join(sorted(_SERIES_FRAGMENT))} "
                f"plus shared-core {', '.join(sorted(_SHARED_CORE))})"
            )
            return None, notes

        series_frag, vcpu, ram_gb, _shared = parsed

        skus = await list_skus(SERVICE_ID_COMPUTE_ENGINE)
        if not skus:
            notes.append(
                "GCP Cloud Billing Catalog SKU list is empty — "
                "auth/network failure (see container logs)"
            )
            return None, notes

        cpu_sku = find_sku(skus, region, (f"{series_frag} Instance Core",))
        ram_sku = find_sku(skus, region, (f"{series_frag} Instance Ram",))
        if cpu_sku is None or ram_sku is None:
            notes.append(
                f"No SKU match for {series_frag} Core/Ram in {region}"
            )
            return None, notes

        cpu_per_hour = first_ondemand_usd(cpu_sku)
        ram_per_hour = first_ondemand_usd(ram_sku)
        if cpu_per_hour is None or ram_per_hour is None:
            notes.append(
                f"Could not extract Core/Ram rates for {series_frag} "
                f"in {region}"
            )
            return None, notes

        monthly = (
            (vcpu * cpu_per_hour + ram_gb * ram_per_hour) * HOURS_PER_MONTH
        )
        notes.append(
            f"machine_type={machine_type} ({vcpu:g} vCPU, {ram_gb:g} GB RAM)"
        )
        return monthly, notes
