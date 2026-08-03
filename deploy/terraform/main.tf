/**
 * Forge Studio — serverless AWS deployment.
 *
 * Two planes, matched to their shapes:
 *   control plane  Lambda (Web Adapter) + API Gateway HTTP API  — scales to zero
 *   execution      Step Functions + ECS Fargate, one task per segment
 *
 * State is DynamoDB + S3 only. That is deliberate: with no RDS there is nothing
 * to put in a private subnet, so Lambda runs outside any VPC and the deployment
 * needs no NAT gateway — which would otherwise be the largest line on the bill
 * (~$33/mo) by an order of magnitude.
 */

terraform {
  # 1.11 is the floor for S3-native state locking (`use_lockfile`); see backend.tf.
  required_version = ">= 1.11.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

locals {
  name = "${var.project}-${var.environment}"
  tags = {
    Project     = var.project
    Environment = var.environment
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ─────────────────────────────────────────────────────────────────────────────
# State: S3 for objects, DynamoDB for documents and the audit stream
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "artifacts" {
  bucket        = "${local.name}-artifacts-${data.aws_caller_identity.current.account_id}"
  force_destroy = var.environment != "prod"
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    # Artifacts are versioned by the engine too; bucket versioning protects
    # against an accidental cleanup wiping decision lineage.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-generated-workspaces"
    status = "Enabled"
    filter { prefix = "workspaces/" }
    expiration { days = var.workspace_retention_days }
    noncurrent_version_expiration { noncurrent_days = 7 }
  }

  rule {
    id     = "expire-uploads"
    status = "Enabled"
    filter { prefix = "uploads/" }
    expiration { days = var.upload_retention_days }
  }
}

# On-demand billing everywhere: this workload is bursty and low volume, so
# provisioned capacity would be pure waste.
locals {
  ddb_tables = {
    workflows   = "key"
    checkpoints = "key"
    memory      = "key"
  }
}

resource "aws_dynamodb_table" "documents" {
  for_each     = local.ddb_tables
  name         = "${local.name}-${each.key}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = each.value

  attribute {
    name = each.value
    type = "S"
  }

  point_in_time_recovery {
    enabled = var.environment == "prod"
  }

  server_side_encryption {
    enabled = true
  }
}

# The audit trace is an ordered stream per workflow, so it needs a sort key.
resource "aws_dynamodb_table" "events" {
  name         = "${local.name}-events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "key"
  range_key    = "seq"

  attribute {
    name = "key"
    type = "S"
  }
  attribute {
    name = "seq"
    type = "N"
  }

  point_in_time_recovery {
    enabled = var.environment == "prod"
  }

  server_side_encryption {
    enabled = true
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# Container images
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_ecr_repository" "api" {
  name                 = "${local.name}-api"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
  force_delete = var.environment != "prod"
}

resource "aws_ecr_repository" "worker" {
  name                 = "${local.name}-worker"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
  force_delete = var.environment != "prod"
}

resource "aws_ecr_lifecycle_policy" "keep_recent" {
  for_each   = { api = aws_ecr_repository.api.name, worker = aws_ecr_repository.worker.name }
  repository = each.value

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# ─────────────────────────────────────────────────────────────────────────────
# Secrets — Parameter Store, not Secrets Manager: standard parameters are free
# and this deployment does not need managed rotation.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_ssm_parameter" "llm_api_key" {
  name  = "/${local.name}/llm-api-key"
  type  = "SecureString"
  value = var.llm_api_key
  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "api_token" {
  name  = "/${local.name}/api-token"
  type  = "SecureString"
  value = var.api_token
  lifecycle { ignore_changes = [value] }
}
