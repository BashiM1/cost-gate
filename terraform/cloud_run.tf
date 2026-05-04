resource "google_cloud_run_v2_service" "cost_gate" {
  name                = var.service_name
  location            = var.region
  deletion_protection = false

  # Initial deploy uses Google's hello placeholder. CI/CD swaps the
  # image via `gcloud run deploy --image=...`, and the lifecycle block
  # below stops Terraform from reverting it on subsequent applies.
  template {
    service_account = google_service_account.runtime.email

    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }

    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name = "SLACK_WEBHOOK_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.runtime["slack-webhook-url"].secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "REMEDIATION_EVENT_BUS_ARN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.runtime["remediation-event-bus-arn"].secret_id
            version = "latest"
          }
        }
      }

      # AWS access happens via Workload Identity Federation: the runtime
      # SA mints a Google ID token, swaps it for AWS temp creds via
      # AssumeRoleWithWebIdentity. Role ARN and SA email are public
      # identifiers, not secrets.
      env {
        name  = "AWS_PRICING_ROLE_ARN"
        value = var.aws_pricing_role_arn
      }

      env {
        name  = "GCP_RUNTIME_SA_EMAIL"
        value = google_service_account.runtime.email
      }

      # Audience claim of the Google ID token sent to AWS STS. Must be
      # in the AWS OIDC provider's client_id_list. SA unique ID is the
      # only value AWS reliably accepts for accounts.google.com.
      env {
        name  = "AWS_FEDERATION_AUDIENCE"
        value = google_service_account.runtime.unique_id
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }

      env {
        name  = "GCP_REGION"
        value = var.region
      }
    }
  }

  # No allUsers invoker binding — the GitHub Action authenticates via
  # Workload Identity Federation and a Google ID token. Add an explicit
  # google_cloud_run_v2_service_iam_member if you need public ingress.

  depends_on = [
    google_project_service.enabled,
    google_secret_manager_secret_iam_member.runtime_accessor,
  ]

  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].containers[0].image,
      traffic,
    ]
  }
}
