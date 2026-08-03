/**
 * Remote state.
 *
 * Locking uses the S3 backend's native lockfile (`use_lockfile`), which holds a
 * conditional-write lock object next to the state itself. That replaces the old
 * DynamoDB lock table — one less resource, one less bill line, and no chance of
 * the lock table and the state bucket drifting out of sync.
 *
 * `key` is per environment. Override it at init time for anything but dev:
 *
 *   terraform init -reconfigure -backend-config="key=forge/prod/terraform.tfstate"
 */

terraform {
  backend "s3" {
    bucket       = "terraform-backend-bucket-085960855786"
    key          = "forge/dev/terraform.tfstate"
    region       = "ap-south-1"
    encrypt      = true
    use_lockfile = true
  }
}
