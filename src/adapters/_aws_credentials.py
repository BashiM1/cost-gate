"""Federated AWS credential acquisition for the cost-gate service.

Cloud Run runs as a GCP service account. Instead of static AWS keys
in Secret Manager, the service mints a Google-signed OIDC token,
presents it to AWS STS via AssumeRoleWithWebIdentity, and uses the
resulting temporary credentials for the AWS Pricing API. Credentials
auto-expire and are cached + refreshed in-process.

Two paths to the Google ID token:

- **Cloud Run / GCE / GKE** — `google.oauth2.id_token.fetch_id_token`
  hits the metadata server, which mints an ID token signed for the
  attached runtime service account. No impersonation, no key file.
- **Local dev** — ADC is a user account; we impersonate the runtime
  SA via `iamcredentials.googleapis.com:generateIdToken`. The user
  must hold `roles/iam.serviceAccountTokenCreator` on the SA.

Both paths produce a JWT with `sub = <SA unique ID>` and `aud =
AWS_FEDERATION_AUDIENCE`. The AWS trust policy validates both.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import aioboto3
import google.auth
import google.auth.transport.requests
import google.oauth2.id_token
from google.auth import impersonated_credentials
from google.auth.exceptions import DefaultCredentialsError, RefreshError

logger = logging.getLogger("cost-gate.aws_creds")

# Audience for the Google ID token. AWS OIDC validation requires this
# to be present in the OIDC provider's client_id_list. AWS applies
# Google-specific format checks that reject arbitrary strings — the
# accepted convention is the runtime SA's numeric unique ID, which is
# also what Google issues as `azp` and `sub`. Set on Cloud Run from
# `google_service_account.runtime.unique_id`.
_AUDIENCE_ENV = "AWS_FEDERATION_AUDIENCE"

_REFRESH_SKEW = timedelta(minutes=5)
_SESSION_DURATION_SECONDS = 3600

_lock = asyncio.Lock()
_cached: dict[str, str] | None = None
_cached_expiry: datetime | None = None


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"missing required env var {name}; cost-gate cannot acquire AWS credentials",
        )
    return value


def _fetch_google_id_token(audience: str) -> str:
    """Mint a Google-signed OIDC token for the runtime SA.

    Tries the metadata server first; falls back to impersonation
    locally. Raises if neither works.
    """
    request = google.auth.transport.requests.Request()
    try:
        return google.oauth2.id_token.fetch_id_token(request, audience)
    except (DefaultCredentialsError, RefreshError) as exc:
        logger.debug(
            "metadata-server ID token unavailable (%s); attempting SA impersonation",
            exc,
        )

    sa_email = _required_env("GCP_RUNTIME_SA_EMAIL")
    source_creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    impersonator = impersonated_credentials.Credentials(
        source_credentials=source_creds,
        target_principal=sa_email,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    id_creds = impersonated_credentials.IDTokenCredentials(
        impersonator,
        target_audience=audience,
        include_email=True,
    )
    id_creds.refresh(request)
    return id_creds.token


async def _assume_role(role_arn: str) -> tuple[dict[str, str], datetime]:
    audience = _required_env(_AUDIENCE_ENV)
    web_token = await asyncio.to_thread(_fetch_google_id_token, audience)
    session = aioboto3.Session()
    async with session.client("sts", region_name="us-east-1") as sts:
        resp = await sts.assume_role_with_web_identity(
            RoleArn=role_arn,
            RoleSessionName="cost-gate-pricing",
            WebIdentityToken=web_token,
            DurationSeconds=_SESSION_DURATION_SECONDS,
        )
    creds = resp["Credentials"]
    return (
        {
            "aws_access_key_id": creds["AccessKeyId"],
            "aws_secret_access_key": creds["SecretAccessKey"],
            "aws_session_token": creds["SessionToken"],
        },
        creds["Expiration"],
    )


async def get_aws_credentials() -> dict[str, str]:
    """Return current temporary AWS credentials, refreshing near expiry.

    Process-wide cache. Concurrent callers share a single refresh.
    """
    global _cached, _cached_expiry
    now = datetime.now(timezone.utc)
    if (
        _cached is not None
        and _cached_expiry is not None
        and (_cached_expiry - now) > _REFRESH_SKEW
    ):
        return _cached

    async with _lock:
        now = datetime.now(timezone.utc)
        if (
            _cached is not None
            and _cached_expiry is not None
            and (_cached_expiry - now) > _REFRESH_SKEW
        ):
            return _cached

        role_arn = _required_env("AWS_PRICING_ROLE_ARN")
        creds, expiry = await _assume_role(role_arn)
        _cached = creds
        _cached_expiry = expiry
        logger.info(
            "AWS pricing credentials refreshed; expires at %s",
            expiry.isoformat(),
        )
        return creds
