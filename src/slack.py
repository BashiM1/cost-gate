"""Slack Block Kit notification for threshold breaches on merge.

Single responsibility: render a CostEstimateResponse as Block Kit and
POST it to an incoming webhook. Never raises — callers get a bool.
"""
from __future__ import annotations

import logging

import aiohttp

from src.models import CostEstimateResponse

logger = logging.getLogger("cost-gate.slack")

_SLACK_TIMEOUT = aiohttp.ClientTimeout(total=10)
_SECTION_TEXT_LIMIT = 2900  # Slack section text caps at 3000; keep headroom.


def _github_pr_url(repository: str, pr_number: int) -> str:
    return f"https://github.com/{repository}/pull/{pr_number}"


def build_breach_message(estimate: CostEstimateResponse) -> list[dict]:
    pr = estimate.pr_context
    pr_url = _github_pr_url(pr.repository, pr.pr_number)

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Cost Gate Alert — Threshold Breached"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Repository*\n<https://github.com/{pr.repository}|{pr.repository}>"},
                {"type": "mrkdwn", "text": f"*Pull Request*\n<{pr_url}|#{pr.pr_number}>"},
                {"type": "mrkdwn", "text": f"*Author*\n{pr.actor}"},
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Net Δ Monthly*\n${estimate.total_monthly_cost_usd:,.2f} "
                        f"(threshold ${estimate.threshold_usd_monthly:,.2f})"
                    ),
                },
            ],
        },
        {"type": "divider"},
    ]

    # Resource breakdown — Slack does not render markdown tables; an
    # aligned plain-text block inside a code fence is the canonical form.
    if estimate.estimates:
        rows = [
            (e.address, e.cloud.value, e.change_action.value, f"${e.monthly_cost_usd:,.2f}", e.confidence.value)
            for e in estimate.estimates
        ]
        widths = [max(len(r[i]) for r in [("Resource", "Cloud", "Action", "Δ Monthly", "Confidence"), *rows]) for i in range(5)]
        header = "  ".join(h.ljust(widths[i]) for i, h in enumerate(("Resource", "Cloud", "Action", "Δ Monthly", "Confidence")))
        body_rows = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows]
        body = "\n".join([header, *body_rows])
        text = f"```\n{body}\n```"
        if len(text) > _SECTION_TEXT_LIMIT:
            text = text[: _SECTION_TEXT_LIMIT - 4] + "\n```"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Review after deployment. Follow-up scheduled."}],
        }
    )
    return blocks


async def post_to_slack(webhook_url: str, blocks: list[dict]) -> bool:
    """POST a Block Kit payload to Slack. Returns True on 2xx, False otherwise."""
    payload = {"blocks": blocks}
    try:
        async with aiohttp.ClientSession(timeout=_SLACK_TIMEOUT) as session:
            async with session.post(webhook_url, json=payload) as resp:
                if 200 <= resp.status < 300:
                    return True
                body = await resp.text()
                logger.warning("Slack webhook returned %d: %s", resp.status, body[:500])
                return False
    except (aiohttp.ClientError, TimeoutError) as exc:
        # Log the exception type only — aiohttp's InvalidURL.__str__ contains
        # the URL itself, which would leak the webhook secret into logs.
        logger.warning("Slack webhook POST failed: %s", type(exc).__name__)
        return False
