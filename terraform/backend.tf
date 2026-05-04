terraform {
  backend "gcs" {
    # Bucket is provisioned by terraform/bootstrap. Pass it on init:
    #   terraform init \
    #     -backend-config="bucket=$(cd bootstrap && terraform output -raw state_bucket_name)"
    prefix = "cost-gate"
  }
}
