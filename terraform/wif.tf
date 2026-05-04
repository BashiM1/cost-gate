data "google_project" "this" {
  project_id = var.project_id

  depends_on = [google_project_service.enabled]
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "${var.service_name}-gh"
  display_name              = "Cost Gate GitHub Actions"
  description               = "Federates GitHub Actions OIDC tokens into a GCP service account for cost-gate CI"

  depends_on = [google_project_service.enabled]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions"
  display_name                       = "GitHub Actions OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
    "attribute.actor"      = "assertion.actor"
  }

  # Without this condition, any GitHub repo's OIDC token would resolve
  # against the pool. Restricting to the configured repo is the
  # difference between "federated identity" and "the entire internet".
  attribute_condition = "assertion.repository == \"${var.github_repo}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# Distinct from the Cloud Run runtime SA — the CI identity only invokes
# the service; it never reads the app's secrets.
resource "google_service_account" "ci" {
  account_id   = "${var.service_name}-ci"
  display_name = "Cost Gate CI invoker"
  description  = "GitHub Actions impersonates this SA via WIF to call the Cloud Run endpoint"

  depends_on = [google_project_service.enabled]
}

resource "google_service_account_iam_member" "ci_workload_identity_user" {
  service_account_id = google_service_account.ci.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.github.workload_identity_pool_id}/attribute.repository/${var.github_repo}"
}

resource "google_cloud_run_v2_service_iam_member" "ci_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.cost_gate.location
  name     = google_cloud_run_v2_service.cost_gate.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.ci.email}"
}
