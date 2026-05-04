terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Chicken-and-egg: this bucket *is* the backend for the parent module,
# so it must be provisioned with local state first. Apply once, then
# init the parent with `-backend-config="bucket=$(terraform output -raw state_bucket_name)"`.
resource "google_storage_bucket" "tfstate" {
  name                        = "${var.project_id}-${var.service_name}-tfstate"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 10
    }
    action {
      type = "Delete"
    }
  }
}
