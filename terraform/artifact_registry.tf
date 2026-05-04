resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = var.service_name
  description   = "Cost gate container images"
  format        = "DOCKER"

  depends_on = [google_project_service.enabled]
}

# Least-privilege image pull, scoped to this repo only (not project-wide).
resource "google_artifact_registry_repository_iam_member" "runtime_pull" {
  project    = google_artifact_registry_repository.images.project
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.runtime.email}"
}
