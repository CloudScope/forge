variable "project" {
  type        = string
  default     = "forge"
  description = "Name prefix for every resource."
}

variable "environment" {
  type        = string
  default     = "dev"
  description = "Environment suffix. 'prod' enables PITR and disables force-destroy."
}

variable "region" {
  type    = string
  default = "us-east-1"
}

# ── Images ───────────────────────────────────────────────────────────────────

variable "image_tag" {
  type        = string
  default     = "latest"
  description = "Tag to deploy from both ECR repositories."
}

# ── Control plane sizing ─────────────────────────────────────────────────────

variable "api_memory_mb" {
  type        = number
  default     = 1024
  description = "Lambda memory. CPU scales with memory; 1 GB keeps cold starts ~1s."
}

variable "api_timeout_s" {
  type        = number
  default     = 30
  description = "The API never runs a workflow, so requests are short by design."
}

# ── Execution sizing ─────────────────────────────────────────────────────────

variable "worker_cpu" {
  type        = number
  default     = 512
  description = "Fargate CPU units (512 = 0.5 vCPU). Agent work is LLM-I/O bound."
}

variable "worker_memory_mb" {
  type    = number
  default = 1024
}

variable "worker_max_workers" {
  type        = number
  default     = 4
  description = "Parallel agent fan-out inside one task (engine ThreadPoolExecutor)."
}

variable "use_fargate_spot" {
  type        = bool
  default     = true
  description = <<-EOT
    Run execution on Fargate Spot (~70% cheaper). Safe here: a Spot interruption
    leaves durable state behind and Step Functions retries the segment, which the
    engine resumes from its last checkpoint.
  EOT
}

variable "segment_timeout_s" {
  type        = number
  default     = 3600
  description = "Max wall-clock for one segment before Step Functions fails it."
}

variable "approval_timeout_s" {
  type        = number
  default     = 604800
  description = "How long a run may wait at a human gate (default 7 days, max 1 year)."
}

# ── Networking ───────────────────────────────────────────────────────────────

variable "vpc_id" {
  type        = string
  default     = ""
  description = "Existing VPC for Fargate. Leave empty to use the default VPC."
}

variable "subnet_ids" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Subnets for Fargate tasks. Leave empty to use the default VPC's subnets.
    Public subnets with assign_public_ip are intentional: tasks need egress to
    the LLM endpoint and ECR, and a NAT gateway would cost more than the entire
    rest of this deployment. The task security group allows no inbound traffic.
  EOT
}

# ── Secrets ──────────────────────────────────────────────────────────────────

variable "llm_api_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = <<-EOT
    Initial value only; rotate in Parameter Store, not in state. Empty stores a
    placeholder the app reads as unset, leaving agents on their heuristic path.
  EOT
}

variable "api_token" {
  type        = string
  default     = ""
  sensitive   = true
  description = <<-EOT
    Studio API bearer token. Empty means the API is loopback-only: a placeholder
    is stored so the parameter exists, and the app refuses it as a credential.
  EOT
}

# ── Retention ────────────────────────────────────────────────────────────────

variable "workspace_retention_days" {
  type        = number
  default     = 30
  description = "Generated code is reproducible; artifacts and audit are kept."
}

variable "upload_retention_days" {
  type    = number
  default = 90
}

variable "log_retention_days" {
  type    = number
  default = 30
}
