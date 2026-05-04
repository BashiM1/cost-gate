# Runtime identity for the Cloud Run service. Kept distinct from any
# CI/CD deployer identity so the deployer can't read app secrets.
resource "google_service_account" "runtime" {
  account_id   = "${var.service_name}-runtime"
  display_name = "Cost Gate Cloud Run runtime"
  description  = "Runtime identity for the cost-gate Cloud Run service"

  depends_on = [google_project_service.enabled]
}
