# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI service hosted on GCP Cloud Run that ingests Terraform plan JSON from a GitHub Action, estimates per-resource monthly cost via the AWS Pricing API and the GCP Cloud Billing Catalog API, and returns a PR-comment-ready breakdown. Pairs with the sibling repo `finops-agentic-remediation` (AWS-side remediation engine) — they're connected via EventBridge and Slack webhook, **not shared code**.

GCP region is pinned to `europe-west2` (London). The Pricing API itself is queried in `us-east-1` (the only region it's hosted in).

## Common commands

```bash
# Build
docker build -t cost-gate:dev .

# Run locally. AWS access is brokered via OIDC federation — there are
# no AWS env-var creds. Locally, ADC is a user account; the user must
# hold roles/iam.serviceAccountTokenCreator on the runtime SA so the
# container can impersonate it. The container runs as uid 10001, so
# ADC must be world-readable — copy first, then mount.
mkdir -p /tmp/cg-creds && cp ~/.config/gcloud/application_default_credentials.json /tmp/cg-creds/adc.json && chmod 644 /tmp/cg-creds/adc.json
docker run -d --name cost-gate-test -p 8080:8080 \
  -v /tmp/cg-creds:/secrets:ro \
  -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/adc.json \
  -e AWS_PRICING_ROLE_ARN="arn:aws:iam::582600397173:role/gcp-cost-gate-pricing" \
  -e GCP_RUNTIME_SA_EMAIL="cost-gate-runtime@finops-governance.iam.gserviceaccount.com" \
  -e AWS_FEDERATION_AUDIENCE="105552136781502150492" \
  cost-gate:dev

# Health and adapter inventory
curl -sS --retry 30 --retry-delay 1 --retry-connrefused --retry-all-errors http://127.0.0.1:8080/healthz
curl -sS http://127.0.0.1:8080/api/v1/adapters

# Cleanup
docker stop cost-gate-test && rm -f /tmp/cg-creds/adc.json && rmdir /tmp/cg-creds

# Terraform
cd terraform/bootstrap && terraform apply -var="project_id=<id>"
cd .. && terraform init -backend-config="bucket=$(cd bootstrap && terraform output -raw state_bucket_name)"
terraform apply -var="project_id=<id>" -var="github_repo=<owner/repo>"
```

## Architecture — the load-bearing details

**Adapter registry pattern.** `src/adapters/registry.py` holds a module-level dict keyed by Terraform `resource_type`. `src/adapters/__init__.py` instantiates and registers each adapter at import time. `src/main.py` does `import src.adapters` (side-effect) and dispatches each `resource_changes[]` entry to its adapter via `asyncio.gather` — adapters run in parallel.

**Two independent axes on every estimate** (this is the design decision most likely to be misread):
- `pricing_basis: PricingBasis` — what kind of pricing model the resource has: `FIXED` (NAT GW), `CONFIG_DEPENDENT` (EC2, RDS, GCE, Cloud SQL), `USAGE_DEPENDENT` (Lambda, S3, Cloud Run).
- `pricing_source: PricingSource` — where the rate came from: `AWS_PRICING_API`, `GCP_BILLING_CATALOG`, `FALLBACK_ESTIMATE`, `STUB`, `UNKNOWN`.

These are NOT collapsible. NAT Gateway is `basis=FIXED, source=AWS_PRICING_API` (we still fetch the rate, but config doesn't change it). Lambda is `basis=USAGE_DEPENDENT, source=AWS_PRICING_API` (rate is real, but the absolute number is illustrative because we assumed 1M invocations × 100ms). Stubs for unmapped types are `basis=USAGE_DEPENDENT, source=STUB`.

**Replace semantics = update semantics.** `monthly_cost_usd = planned - prior` for both. Both lookups must succeed (`lookup_failed=True` if either is None). Prior cost lives in `monthly_cost_prior_usd` — **never** stuffed into a free-text note. The earliest implementation got this wrong (replace returned `+planned`, prior went into a note); fixed in commit history. If you re-introduce a "Replaces prior X billed at $Y/mo" note, you've reverted the fix.

**GCP catalog SKU caching.** `_gcp_common.list_skus()` paginates an entire service's SKU list (Compute Engine has 25k+, Cloud SQL 5k+) and caches it in a module-level dict for the process lifetime. First request after container restart pays ~5–10s; subsequent requests hit memory. The token is cached separately in `_get_access_token()` until ~60s before expiry.

**Why raw aiohttp instead of `google-cloud-billing`** (documented in `_gcp_common.py` head comment): the official SDK is synchronous. Calling it from an async adapter would block the event loop and serialise the `asyncio.gather` fan-out. aiohttp keeps the adapter interface end-to-end async. `google.auth` is used for ADC token minting (sync, but fast and cached).

## Coverage limits — known gaps that surface as `confidence=none`

These are intentional cuts, not bugs. Each returns a helpful note explaining what's missing:

- **`aws_db_instance`**: open-source engines only (MySQL, PostgreSQL, MariaDB). Oracle and SQL Server need `licenseModel` + `databaseEdition` filter handling.
- **`aws_instance`**: tenancy mapped from Terraform's `default`/`dedicated`/`host`; default OS = Linux, license = `No license required`. Spot/RI never considered.
- **`google_compute_instance`**: machine series e2/n1/n2/n2d/c2/c2d/t2d/t2a + e2 shared-core (e2-micro/small/medium). A2 GPU, M-series, and others return `confidence=none`. Shared-core e2-medium is approximated by multiplying regular E2 vCPU/RAM rates rather than the bundled SKU — overestimates by ~30%.
- **`google_sql_database_instance`**: `db-custom-N-M` form only. Legacy `db-n1-standard-N` and `db-f1-micro`/`db-g1-small` are skipped. Storage cost is excluded (note surfaces this).
- **Region propagation**: AWS resources don't have `region` in `change.after` by default (it's a provider-level setting); some GCP resources only carry `zone` or `location`. The Cloud Run request schema accepts a `provider_regions: dict[str, str]` field (keys are Terraform provider names — `aws`, `google`, `azurerm`). Before adapter dispatch, `src/main.py:_inject_region` resolves the right entry per resource and calls `setdefault("region", ...)` (and `setdefault("location", ...)` for Google) on `change.after`. The CI Action extracts the dict from `configuration.provider_config.<provider>.expressions.region.constant_value` and passes it through unchanged — the Action does not need to know which resource belongs to which provider. Adapters still fall back gracefully when region is missing entirely (e.g. for an unmapped provider).

## Things that will trip you up

**The container is not invocable from `allUsers`.** Cloud Run service has no `roles/run.invoker` binding by default. The GitHub Action authenticates via Workload Identity Federation (`terraform/wif.tf`): the OIDC provider's `attribute_condition` restricts trust to the configured `var.github_repo`, and the CI service account `cost-gate-ci@…` has the `roles/run.invoker` binding.

**Two distinct WIF flows — don't conflate them.** (1) **GitHub Actions → GCP**: GitHub OIDC token → GCP STS → temp creds for the CI SA, used to invoke Cloud Run. Wired in `terraform/wif.tf`. (2) **GCP → AWS**: the runtime SA mints a Google ID token, hands it to AWS STS via `AssumeRoleWithWebIdentity`, gets temp keys for the AWS Pricing API. Wired in `src/adapters/_aws_credentials.py` against the IAM role provisioned out-of-band from `aws/trust-policy.json`+`aws/permissions-policy.json`. The two flows share no state — keep them mentally separate.

**No static AWS credentials anywhere in the system.** This is the load-bearing security invariant of the design: there are no `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` env vars on Cloud Run, no AWS keys in Secret Manager. Reverting to env-var creds (because federation seems harder to debug) breaks the architecture's security claim. If you ever need temporarily — flag it, don't merge it.

**Terraform deploys a placeholder image** (`us-docker.pkg.dev/cloudrun/container/hello`) and uses `lifecycle { ignore_changes = [client, client_version, template[0].containers[0].image, traffic] }`. The GitHub Action / `gcloud run deploy --image=...` swaps the image and reshapes traffic; Terraform won't fight it. Don't remove the `ignore_changes` block.

**Bootstrap before main.** `terraform/bootstrap/` provisions the GCS state bucket using local state. The parent module's `backend.tf` is a partial GCS config — `terraform init` in `terraform/` requires `-backend-config="bucket=<from bootstrap output>"`. Running `terraform init` without that flag or without the bucket existing will fail.

**Local testing requires ADC + credential dance + SA impersonation.** See the run command above. The non-root container user (`uid 10001`) can't read `~/.config/gcloud/application_default_credentials.json` directly because it's `chmod 600` for the host user — copy to `/tmp/cg-creds` with `chmod 644` first. Additionally, AWS calls go through OIDC federation: locally the developer's user account ADC must be able to impersonate the runtime SA (`gcloud iam service-accounts add-iam-policy-binding cost-gate-runtime@… --member="user:<email>" --role="roles/iam.serviceAccountTokenCreator"` once per developer). Don't set `--rm` on the docker run if you want logs after a crash.

**Vendored dependency: `requests`.** `requirements.txt` pins `requests` not because we use it directly but because `google.auth.transport.requests` (the sync transport google-auth ships with) needs it. Removing it breaks GCP token refresh.

## What's not done yet

- **GitHub Action** — designed but not built. Will need WIF setup in Terraform first.
- **Real GCP project deployment** — Terraform partially applied to `finops-governance` (us-central1) on 2026-05-04. WIF, SAs, secrets (empty), Artifact Registry, IAM bindings all created; Cloud Run service create blocked on empty secret versions (see "Things that will trip you up" entry below). Outputs (Cloud Run URL, Artifact Registry URI, runtime SA email) defined.
- **Secret Manager population** — two empty secrets created (`slack-webhook-url`, `remediation-event-bus-arn`). Populate via `gcloud secrets versions add` before first real deploy. AWS access does NOT live in Secret Manager — see the federation note above.
- **PR-comment posting** — endpoint returns `summary_markdown` ready to drop into a PR, but nothing posts it yet. The GitHub Action will.
- **EventBridge fanout to remediation engine** — the env var `REMEDIATION_EVENT_BUS_ARN` exists but no adapter or middleware fires events on threshold breach.
