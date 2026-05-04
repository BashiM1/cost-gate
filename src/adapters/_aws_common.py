"""Shared helpers for AWS Pricing API adapters.

Pure utilities — no per-resource logic lives here. Each adapter
imports what it needs and keeps its own action-dispatch shape so the
"how this adapter works" reads top-to-bottom in one file.
"""
from __future__ import annotations

import json
import logging

import aioboto3
from botocore.exceptions import BotoCoreError, ClientError

from src.adapters._aws_credentials import get_aws_credentials

logger = logging.getLogger("cost-gate.aws_common")

PRICING_API_REGION = "us-east-1"
HOURS_PER_MONTH = 730

# Pricing API uses human "location" names; map from Terraform region codes.
REGION_TO_LOCATION: dict[str, str] = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "ca-central-1": "Canada (Central)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-north-1": "EU (Stockholm)",
    "eu-south-1": "EU (Milan)",
    "eu-south-2": "EU (Spain)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "sa-east-1": "South America (Sao Paulo)",
}


def region_to_location(region: str | None) -> str | None:
    if not region:
        return None
    return REGION_TO_LOCATION.get(region)


async def get_products(
    service_code: str,
    filters: list[dict],
    max_results: int = 1,
) -> list[dict]:
    """Call AWS Pricing API and decode the JSON-string PriceList entries.

    Returns an empty list on any failure — callers must not rely on
    exceptions to signal "no price found".
    """
    try:
        creds = await get_aws_credentials()
        session = aioboto3.Session(**creds)
        async with session.client(
            "pricing", region_name=PRICING_API_REGION,
        ) as client:
            resp = await client.get_products(
                ServiceCode=service_code,
                Filters=filters,
                MaxResults=max_results,
            )
    except (BotoCoreError, ClientError) as e:
        logger.warning("Pricing API call failed (service=%s): %s", service_code, e)
        return []

    items: list[dict] = []
    for raw in resp.get("PriceList") or []:
        try:
            items.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Pricing API decode failed: %s", e)
    return items


def first_ondemand_usd(price_item: dict) -> float | None:
    """Pull the first on-demand USD rate from a Pricing SKU record."""
    try:
        terms = price_item["terms"]["OnDemand"]
        first_term = next(iter(terms.values()))
        first_dim = next(iter(first_term["priceDimensions"].values()))
        return float(first_dim["pricePerUnit"]["USD"])
    except (KeyError, StopIteration, ValueError, TypeError):
        return None
