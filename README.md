# Cost Gate

Multi-cloud cost-aware CI/CD gate for Terraform plans.

## What It Does

Estimates per-resource monthly cost for AWS and GCP resources in Terraform PRs using real pricing APIs (AWS Pricing API, GCP Cloud Billing Catalog). Posts a cost breakdown as a PR comment on every plan. On merge above a configurable threshold, alerts the FinOps Slack channel and fires a `CostGateThresholdExceeded` event to the FinOps remediation engine for follow-up review.

## Architecture

- FastAPI on GCP Cloud Run (`us-central1`).
- 8 pricing adapters: `aws_instance`, `aws_db_instance`, `aws_lambda_function`, `aws_s3_bucket`, `aws_nat_gateway`, `google_compute_instance`, `google_sql_database_instance`, `google_cloud_run_v2_service`. Each is a single file under `src/adapters/`, registered at import time.
- GitHub Action triggers on PR open / synchronise / close. On open and synchronise: plans `examples/demo-tf/`, POSTs to the service, posts the cost breakdown as a PR comment. On merge above threshold: also fires Slack + an EventBridge event onto the FinOps hub bus.
- **Zero static credentials.** Two distinct OIDC flows:
  - GitHub Actions → GCP via Workload Identity Federation (`terraform/wif.tf`); the runner exchanges its OIDC token for a short-lived GCP ID token tied to the `cost-gate-ci` service account, then uses it as a Cloud Run identity-token bearer.
  - GCP runtime → AWS via `AssumeRoleWithWebIdentity` against an IAM OIDC provider for `accounts.google.com` (`aws/trust-policy.json`); the Cloud Run runtime SA federates into a least-privileged role (`pricing:GetProducts`, `events:PutEvents`).
- **Two-axis pricing model.** Each estimate carries `pricing_source` (where the rate came from: `aws_pricing_api`, `gcp_billing_catalog`, `fallback_estimate`, `stub`, `unknown`) **and** `pricing_basis` (what kind of pricing the resource has: `fixed`, `config_dependent`, `usage_dependent`). The two axes are independent — NAT Gateway is `basis=fixed, source=aws_pricing_api`; an unmapped resource is `basis=usage_dependent, source=stub`. See `CLAUDE.md` (local) for the rationale.

## Security

- Cloud Run service has no `roles/run.invoker` for `allUsers`. The GitHub Action authenticates via WIF; no shared secret.
- Cross-cloud API calls use federated identity with ephemeral tokens (1-hour expiry, in-process refresh in `src/adapters/_aws_credentials.py`).
- No AWS credentials in GCP Secret Manager or environment variables. The only secrets in Secret Manager are `slack-webhook-url` and `remediation-event-bus-arn` — both are non-credential identifiers / opaque webhooks.
- Audience pinning: the AWS IAM trust policy condition fixes `accounts.google.com:sub` to the runtime SA's numeric unique ID. Other GCP identities cannot federate into the role even from the same project.

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI, Uvicorn |
| AWS clients | aioboto3 (async), boto3 |
| GCP clients | aiohttp + Cloud Billing Catalog REST |
| Auth | `google-auth` (GCP→AWS federation), aws_lambda_powertools (none — direct REST) |
| IaC | Terraform |
| Container | Docker, Artifact Registry |
| Hosting | Cloud Run (us-central1) |
| Secrets | GCP Secret Manager (Slack + remediation bus only) |
| CI | GitHub Actions, Workload Identity Federation |

## Related Projects

- **[`finops-agentic-remediation`](https://github.com/BashiM1/finops-agentic-remediation)** — the downstream remediation engine that consumes `CostGateThresholdExceeded` events and executes approved rightsizing actions. The two repos share no code; they are connected operationally via the `REMEDIATION_EVENT_BUS_ARN` environment variable on Cloud Run and the same Slack workspace.

## Quick Start

```bash
# 1. Bootstrap state bucket (once per environment)
cd terraform/bootstrap
terraform init && terraform apply -var="project_id=<PROJECT_ID>"

# 2. Init the main module against the bootstrap-created bucket
cd ..
terraform init -backend-config="bucket=$(cd bootstrap && terraform output -raw state_bucket_name)"
terraform apply -var="project_id=<PROJECT_ID>" -var="github_repo=<OWNER/REPO>"

# 3. Populate the two runtime secrets (Cloud Run mounts these as env vars)
printf 'https://hooks.slack.com/services/...' \
  | gcloud secrets versions add slack-webhook-url --data-file=- --project=<PROJECT_ID>
printf 'arn:aws:events:eu-west-2:<AWS_ACCOUNT>:event-bus/finops-hub-bus' \
  | gcloud secrets versions add remediation-event-bus-arn --data-file=- --project=<PROJECT_ID>

# 4. Build, push, deploy
docker build -t us-central1-docker.pkg.dev/<PROJECT_ID>/cost-gate/cost-gate:latest .
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
docker push us-central1-docker.pkg.dev/<PROJECT_ID>/cost-gate/cost-gate:latest
gcloud run deploy cost-gate \
  --image=us-central1-docker.pkg.dev/<PROJECT_ID>/cost-gate/cost-gate:latest \
  --region=us-central1 --project=<PROJECT_ID>

# 5. Wire the GitHub Action (set repository variables, no secrets)
gh variable set WIF_PROVIDER         --body "$(terraform -chdir=terraform output -raw wif_provider_name)"
gh variable set CI_SERVICE_ACCOUNT   --body "$(terraform -chdir=terraform output -raw ci_service_account_email)"
gh variable set COST_GATE_URL        --body "$(terraform -chdir=terraform output -raw cloud_run_url)"

# 6. Open a PR touching examples/demo-tf/ — the workflow runs, posts a comment.
#    Merge above the configured threshold to fire the Slack + EventBridge path.
```

The reverse direction (AWS→GCP) federation used by the remediation engine's notifier is configured in `terraform/wif_aws_bridge.tf` and applies as part of step 2 above.
