from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class PricingSource(str, Enum):
    AWS_PRICING_API = "aws_pricing_api"
    GCP_BILLING_CATALOG = "gcp_billing_catalog"
    FALLBACK_ESTIMATE = "fallback_estimate"
    STUB = "stub"
    UNKNOWN = "unknown"


class CloudProvider(str, Enum):
    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"
    UNKNOWN = "unknown"


class ChangeAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    REPLACE = "replace"
    READ = "read"
    NO_OP = "no-op"


class Confidence(str, Enum):
    HIGH = "high"      # adapter returned a real Pricing API match
    MEDIUM = "medium"  # adapter returned an estimate (e.g. fallback heuristic)
    LOW = "low"        # rough guess
    NONE = "none"      # adapter ran but could not compute a number


class PricingBasis(str, Enum):
    """What kind of pricing model a resource has.

    Independent of `pricing_source` (which records *where* the rate
    came from). A resource can be FIXED with source=AWS_PRICING_API
    (e.g. NAT Gateway hourly), CONFIG_DEPENDENT with source=
    GCP_BILLING_CATALOG (e.g. n2-standard-4), or USAGE_DEPENDENT with
    source=FALLBACK_ESTIMATE (e.g. Lambda).
    """
    FIXED = "fixed"                        # hourly/monthly rate doesn't depend on resource config
    CONFIG_DEPENDENT = "config_dependent"  # rate is a function of declared config
    USAGE_DEPENDENT = "usage_dependent"    # cost is a function of runtime usage; plan can't predict


class PRContext(BaseModel):
    repository: str = Field(description="GitHub repository in owner/repo form")
    pr_number: int = Field(gt=0)
    head_sha: str = Field(min_length=7, max_length=40)
    base_ref: str = "main"
    actor: str = Field(description="GitHub login of the user opening/updating the PR")


class CostEstimateRequest(BaseModel):
    pr_context: PRContext
    terraform_plan: dict[str, Any] = Field(
        description="Output of `terraform show -json plan.bin`",
    )
    threshold_usd_monthly: float = Field(default=100.0, gt=0)
    provider_regions: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of Terraform provider name (e.g. 'aws', 'google') to default "
            "region. Used to resolve a region for resources whose `change.after` "
            "doesn't carry one (AWS resources never do; some GCP resources only "
            "carry zone or location). The CI Action extracts this from "
            "`configuration.provider_config.<provider>.expressions.region` and "
            "passes it through unchanged."
        ),
    )


class ResourceCostEstimate(BaseModel):
    address: str
    resource_type: str
    cloud: CloudProvider
    region: str | None = None
    change_action: ChangeAction
    # monthly_cost_usd is the *delta* this change introduces:
    #   create  → +planned
    #   delete  → -prior
    #   update  → planned - prior
    #   replace → +planned (with prior surfaced in notes)
    monthly_cost_usd: float
    monthly_cost_prior_usd: float | None = None
    monthly_cost_planned_usd: float | None = None
    pricing_source: PricingSource
    pricing_basis: PricingBasis
    confidence: Confidence = Confidence.NONE
    notes: list[str] = Field(default_factory=list)


class CostEstimateResponse(BaseModel):
    request_id: str
    pr_context: PRContext
    estimates: list[ResourceCostEstimate]
    total_monthly_cost_usd: float
    threshold_usd_monthly: float
    threshold_breached: bool
    summary_markdown: str
    timestamp: str


class HealthResponse(BaseModel):
    status: str
    version: str
