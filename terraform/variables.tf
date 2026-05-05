variable "project_id" {
  description = "GCP project ID hosting the cost-gate stack"
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Run, Artifact Registry, and Secret Manager replication"
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name; also used as the Artifact Registry repository ID"
  type        = string
  default     = "cost-gate"
}

variable "github_repo" {
  description = "GitHub repo (owner/repo) allowed to mint identity tokens via WIF. Used in the Workload Identity Pool provider's attribute_condition to restrict OIDC trust to this repo only."
  type        = string
}

variable "aws_pricing_role_arn" {
  description = "ARN of the AWS IAM role the Cloud Run service assumes via OIDC web-identity federation to call the AWS Pricing API. Provisioned out-of-band via aws/trust-policy.json + aws/permissions-policy.json (see CLAUDE.md). Not a secret — it's a public resource identifier."
  type        = string
  default     = "arn:aws:iam::582600397173:role/gcp-cost-gate-pricing"
}

variable "aws_account_id" {
  description = "AWS account hosting the finops-agentic-remediation stack. Used by the AWS→GCP WIF provider to scope trust to a single AWS account."
  type        = string
  default     = "582600397173"
}

variable "aws_notifier_role_name" {
  description = "IAM role name (not ARN) of the AWS Lambda permitted to federate into the aws-recommender-reader SA. Pinned in the WIF provider's attribute_condition; changing the AWS-side role name requires re-applying TF."
  type        = string
  default     = "finops-followup-notifier-role"
}
