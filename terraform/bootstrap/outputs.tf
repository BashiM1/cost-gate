output "state_bucket_name" {
  description = "Name of the GCS bucket that holds Terraform state for the parent module"
  value       = google_storage_bucket.tfstate.name
}
