terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

# This module is plan-only — it exists so the GitHub Action has something
# to `terraform plan` and feed into cost-gate. We never apply it; the
# real demo resources are deployed via CLI (see CLAUDE.md). The
# skip_* flags on the AWS provider let `terraform plan` succeed in CI
# without any AWS credentials, since none of the resources here use
# data sources that would call the API.
provider "aws" {
  region                      = "eu-west-2"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
  skip_requesting_account_id  = true
}

provider "google" {
  project = "finops-governance"
  region  = "us-central1"
}

resource "aws_instance" "demo" {
  ami           = "ami-00000000000000000"
  instance_type = "t3.small"
  tenancy       = "default"

  tags = {
    Name           = "cost-gate-demo-plan"
    "FinOps-Managed" = "True"
    Environment    = "Dev"
    Purpose        = "CostGateDemo"
  }
}

resource "google_compute_instance" "demo" {
  name         = "cost-gate-demo-plan"
  machine_type = "e2-small"
  zone         = "us-central1-a"

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
    }
  }

  network_interface {
    network = "default"
    access_config {}
  }

  labels = {
    finops-managed = "true"
    environment    = "dev"
    purpose        = "cost-gate-demo"
  }
}
