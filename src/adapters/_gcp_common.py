"""Shared helpers for GCP Cloud Billing Catalog API adapters.

Auth uses Application Default Credentials. On Cloud Run that resolves
to the runtime service account; locally, point at an ADC file via
GOOGLE_APPLICATION_CREDENTIALS or run `gcloud auth application-default
login`.

The Catalog API has tens of thousands of SKUs per service. This module
fetches each service's SKU list **once per process** and serves all
subsequent lookups from memory. Cache lives until the process restarts.
"""

# We talk to the Catalog REST endpoint over aiohttp and refresh ADC
# tokens by hand rather than using the official google-cloud-billing
# SDK. That SDK is synchronous; calling it from an async adapter would
# block the event loop and serialise the asyncio.gather fan-out across
# every adapter. Raw aiohttp keeps the adapter interface end-to-end async.
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
import google.auth
import google.auth.exceptions
from google.auth.transport.requests import Request as GoogleAuthRequest

logger = logging.getLogger("cost-gate.gcp_common")

CATALOG_BASE = "https://cloudbilling.googleapis.com/v1"
HOURS_PER_MONTH = 730
GCP_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# Hardcoded GCP-assigned service IDs (public, stable).
SERVICE_ID_COMPUTE_ENGINE = "6F81-5844-456A"
SERVICE_ID_CLOUD_SQL = "9662-B51E-5089"
SERVICE_ID_CLOUD_RUN = "152E-C115-5142"

_sku_caches: dict[str, list[dict]] = {}
_cache_lock = asyncio.Lock()
_cached_token: dict[str, Any] = {"token": None, "expires": 0.0}
_token_lock = asyncio.Lock()


def region_from_zone(zone: str | None) -> str | None:
    """Strip the last `-<letter>` segment to convert zone → region."""
    if not zone:
        return None
    parts = zone.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isalpha() and len(parts[1]) == 1:
        return parts[0]
    return zone


def first_ondemand_usd(sku: dict) -> float | None:
    """Pull the on-demand USD/unit rate from a SKU's tieredRates list."""
    try:
        pricing_info = sku.get("pricingInfo") or []
        if not pricing_info:
            return None
        rates = pricing_info[0]["pricingExpression"]["tieredRates"]
        if not rates:
            return None
        # The last tier is the steady-state rate (lower tiers are
        # ramp-up bands; some SKUs have only one tier).
        last = rates[-1]["unitPrice"]
        units = int(last.get("units", "0"))
        nanos = int(last.get("nanos", 0))
        return units + nanos / 1e9
    except (KeyError, ValueError, TypeError):
        return None


def find_sku(
    skus: list[dict],
    region: str,
    description_contains_all: tuple[str, ...],
    *,
    require_usage_type: str = "OnDemand",
    description_excludes: tuple[str, ...] = (
        "Spot", "Preemptible", "Commitment",
    ),
) -> dict | None:
    """First SKU whose region matches, usage type matches, and whose
    description contains every fragment in ``description_contains_all``
    while containing none of the excluded fragments.

    Excludes Spot/Preemptible/Commitment SKUs by default — those have
    similar descriptions to OnDemand and would otherwise sometimes win.
    """
    for s in skus:
        if region not in (s.get("serviceRegions") or []):
            continue
        cat = s.get("category") or {}
        if cat.get("usageType") != require_usage_type:
            continue
        desc = s.get("description", "")
        if any(x in desc for x in description_excludes):
            continue
        if all(t in desc for t in description_contains_all):
            return s
    return None


async def list_skus(service_id: str) -> list[dict]:
    """Return every SKU for a service, fetched once and cached."""
    cached = _sku_caches.get(service_id)
    if cached is not None:
        return cached

    async with _cache_lock:
        cached = _sku_caches.get(service_id)
        if cached is not None:
            return cached

        token = await _get_access_token()
        if token is None:
            return []

        skus: list[dict] = []
        page_token = ""
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {token}"}, timeout=timeout,
        ) as session:
            while True:
                url = (
                    f"{CATALOG_BASE}/services/{service_id}/skus"
                    f"?pageSize=5000"
                )
                if page_token:
                    url += f"&pageToken={page_token}"
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            logger.warning(
                                "GCP Catalog list_skus(%s) HTTP %s: %s",
                                service_id, resp.status, body[:200],
                            )
                            return skus
                        data = await resp.json()
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(
                        "GCP Catalog list_skus(%s) error: %s", service_id, e,
                    )
                    return skus

                skus.extend(data.get("skus") or [])
                page_token = data.get("nextPageToken") or ""
                if not page_token:
                    break

        _sku_caches[service_id] = skus
        logger.info(
            "GCP Catalog: cached %d SKUs for service %s",
            len(skus), service_id,
        )
        return skus


async def _get_access_token() -> str | None:
    """Fetch and cache an ADC access token."""
    now = time.time()
    if _cached_token["token"] and _cached_token["expires"] > now + 60:
        return _cached_token["token"]

    async with _token_lock:
        if _cached_token["token"] and _cached_token["expires"] > now + 60:
            return _cached_token["token"]
        try:
            creds, _ = await asyncio.to_thread(
                google.auth.default, scopes=[GCP_SCOPE],
            )
            await asyncio.to_thread(creds.refresh, GoogleAuthRequest())
        except google.auth.exceptions.DefaultCredentialsError as e:
            logger.warning("GCP ADC unavailable: %s", e)
            return None
        except Exception as e:  # pragma: no cover — defensive
            logger.warning("GCP ADC token refresh failed: %s", e)
            return None

        _cached_token["token"] = creds.token
        _cached_token["expires"] = (
            creds.expiry.timestamp() if creds.expiry else now + 3000
        )
        return creds.token
