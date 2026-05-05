# Reverse-direction federation: AWS Lambda IAM identities into GCP.
# Used by the finops-agentic-remediation followup_notifier Lambda to
# call the GCP Recommender API for the demo GCE instance.
#
# Distinct from wif.tf, which federates GitHub Actions OIDC into GCP.
# Different source identity (AWS), different target SA, different pool.

resource "google_iam_workload_identity_pool" "aws_finops_bridge" {
  workload_identity_pool_id = "aws-finops-bridge"
  display_name              = "AWS FinOps bridge"
  description               = "Federates AWS Lambda IAM identities into GCP for the finops remediation engine"

  depends_on = [google_project_service.enabled]
}

resource "google_iam_workload_identity_pool_provider" "aws_finops_bridge" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.aws_finops_bridge.workload_identity_pool_id
  workload_identity_pool_provider_id = "aws-account-${var.aws_account_id}"
  display_name                       = "AWS account ${var.aws_account_id}"

  attribute_mapping = {
    "google.subject"     = "assertion.arn"
    "attribute.account"  = "assertion.account"
    "attribute.aws_role" = "assertion.arn.extract('assumed-role/{role}/')"
  }

  # Without this condition, ANY assumed-role ARN in the trusted account
  # could federate. Restricting to the notifier role's name is the
  # difference between "AWS account 5826… can call us" and "this
  # specific Lambda role can call us".
  attribute_condition = "attribute.account == \"${var.aws_account_id}\" && attribute.aws_role == \"${var.aws_notifier_role_name}\""

  aws {
    account_id = var.aws_account_id
  }
}

resource "google_service_account" "aws_recommender_reader" {
  account_id   = "aws-recommender-reader"
  display_name = "AWS Recommender reader (federated)"
  description  = "Service account AWS Lambdas impersonate via WIF to read GCP Recommender data; least-privilege roles/recommender.viewer at project scope"

  depends_on = [google_project_service.enabled]
}

resource "google_project_iam_member" "aws_recommender_reader_recommender_viewer" {
  project = var.project_id
  role    = "roles/recommender.viewer"
  member  = "serviceAccount:${google_service_account.aws_recommender_reader.email}"
}

resource "google_service_account_iam_member" "aws_recommender_reader_workload_identity_user" {
  service_account_id = google_service_account.aws_recommender_reader.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.aws_finops_bridge.workload_identity_pool_id}/attribute.aws_role/${var.aws_notifier_role_name}"
}
