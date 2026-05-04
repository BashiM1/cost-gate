"""Emit `CostGateThresholdExceeded` events to the FinOps EventBridge bus.

Single responsibility: build the Detail JSON from a CostEstimateResponse
and call PutEvents using the same federated AWS credentials that the
pricing adapters use. The bus ARN comes from REMEDIATION_EVENT_BUS_ARN;
the bus is in eu-west-2 by convention.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import aioboto3
from botocore.exceptions import BotoCoreError, ClientError

from src.adapters._aws_credentials import get_aws_credentials
from src.models import CostEstimateResponse

logger = logging.getLogger("cost-gate.eventbridge")

EVENT_SOURCE = "cost-gate"
EVENT_DETAIL_TYPE = "CostGateThresholdExceeded"
# The bus is region-pinned: arn:aws:events:eu-west-2:<acct>:event-bus/<name>.
_BUS_REGION = "eu-west-2"


def _build_detail(estimate: CostEstimateResponse, merged_at: str) -> dict:
    pr = estimate.pr_context
    return {
        "pr_number": pr.pr_number,
        "repository": pr.repository,
        "author": pr.actor,
        "head_sha": pr.head_sha,
        "total_monthly_cost_usd": estimate.total_monthly_cost_usd,
        "threshold_usd_monthly": estimate.threshold_usd_monthly,
        "review_after_days": estimate.review_after_days,
        "merged_at": merged_at,
        "resources": [
            {
                "address": e.address,
                "resource_type": e.resource_type,
                "cloud": e.cloud.value,
                "region": e.region,
                "change_action": e.change_action.value,
                "monthly_cost_usd": e.monthly_cost_usd,
                "confidence": e.confidence.value,
            }
            for e in estimate.estimates
        ],
    }


async def emit_threshold_exceeded(estimate: CostEstimateResponse) -> bool:
    """Emit a CostGateThresholdExceeded event. Returns True on success.

    Failures (missing config, AWS error, JSON encoding error) are
    logged and surface as a False return — never raised.
    """
    bus_arn = os.environ.get("REMEDIATION_EVENT_BUS_ARN")
    if not bus_arn:
        logger.warning("REMEDIATION_EVENT_BUS_ARN not set; skipping PutEvents")
        return False

    merged_at = datetime.now(timezone.utc).isoformat()
    detail = _build_detail(estimate, merged_at)

    try:
        creds = await get_aws_credentials()
        session = aioboto3.Session(**creds)
        async with session.client("events", region_name=_BUS_REGION) as events:
            resp = await events.put_events(
                Entries=[
                    {
                        "Source": EVENT_SOURCE,
                        "DetailType": EVENT_DETAIL_TYPE,
                        "EventBusName": bus_arn,
                        "Detail": json.dumps(detail),
                    }
                ]
            )
    except (BotoCoreError, ClientError, ValueError, TypeError) as exc:
        logger.warning("PutEvents failed: %s", type(exc).__name__)
        return False

    failed = resp.get("FailedEntryCount", 0)
    if failed:
        # PutEvents accepts the call but rejects entries individually;
        # surface the first error code without leaking the bus ARN.
        first = (resp.get("Entries") or [{}])[0]
        logger.warning(
            "PutEvents had %d failed entry: code=%s",
            failed,
            first.get("ErrorCode", "unknown"),
        )
        return False

    event_id = (resp.get("Entries") or [{}])[0].get("EventId", "unknown")
    logger.info(
        "PutEvents ok: source=%s detail_type=%s event_id=%s pr=%s",
        EVENT_SOURCE,
        EVENT_DETAIL_TYPE,
        event_id,
        estimate.pr_context.pr_number,
    )
    return True
