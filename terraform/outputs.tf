output "cloud_run_url" {
  description = "HTTPS URL of the Cloud Run service"
  value       = google_cloud_run_v2_service.cost_gate.uri
}

output "cloud_run_service_account_email" {
  description = "Email of the runtime service account"
  value       = google_service_account.runtime.email
}

output "artifact_registry_repository_uri" {
  description = "Artifact Registry Docker repository URI for image pushes"
  value       = "${google_artifact_registry_repository.images.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "secret_ids" {
  description = "Secret Manager secret IDs that must be populated before deploy"
  value       = [for s in google_secret_manager_secret.runtime : s.secret_id]
}

output "wif_provider_name" {
  description = "Full resource name of the GitHub Actions WIF provider; pass to google-github-actions/auth as workload_identity_provider"
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "ci_service_account_email" {
  description = "Email of the CI SA that the GitHub Action impersonates"
  value       = google_service_account.ci.email
}

output "aws_finops_bridge_provider_resource_name" {
  description = "Full WIF provider resource path for AWS→GCP. Pass to the AWS notifier Lambda as GCP_WIF_PROVIDER_RESOURCE_NAME."
  value       = google_iam_workload_identity_pool_provider.aws_finops_bridge.name
}

output "aws_recommender_reader_sa_email" {
  description = "Service account the AWS Lambda impersonates via WIF to call GCP Recommender."
  value       = google_service_account.aws_recommender_reader.email
}
