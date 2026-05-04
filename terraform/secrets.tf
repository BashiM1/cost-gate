locals {
  # Empty Secret Manager secrets; populate versions out-of-band before deploy:
  #   echo -n "$VALUE" | gcloud secrets versions add <id> --data-file=-
  # AWS access is brokered via Workload Identity Federation
  # (see terraform/iam_pricing_federation.tf and aws/trust-policy.json) —
  # there are no static AWS keys in Secret Manager.
  runtime_secrets = [
    "slack-webhook-url",
    "remediation-event-bus-arn",
  ]
}

resource "google_secret_manager_secret" "runtime" {
  for_each = toset(local.runtime_secrets)

  secret_id = each.key

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

# Scoped accessor — runtime SA can read only these specific secrets.
resource "google_secret_manager_secret_iam_member" "runtime_accessor" {
  for_each = google_secret_manager_secret.runtime

  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}
