variable "project_id" {
  description = "GCP project ID hosting the state bucket"
  type        = string
}

variable "region" {
  description = "Region for the state bucket (single-region; matches the parent module)"
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Used in the bucket name to keep state isolated per stack"
  type        = string
  default     = "cost-gate"
}
