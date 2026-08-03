output "studio_url" {
  value       = aws_apigatewayv2_stage.default.invoke_url
  description = "Open this to reach the Studio. Append ?token=<api_token> once to set the cookie."
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.workflow.arn
}

output "artifacts_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "ddb_table_prefix" {
  value = local.name
}

output "ecr_api_repository" {
  value = aws_ecr_repository.api.repository_url
}

output "ecr_worker_repository" {
  value = aws_ecr_repository.worker.repository_url
}

output "push_images" {
  description = "Build and push both images, then re-apply with the new tag."
  value       = <<-EOT
    aws ecr get-login-password --region ${var.region} \
      | docker login --username AWS --password-stdin ${split("/", aws_ecr_repository.api.repository_url)[0]}

    docker build -f deploy/docker/Dockerfile.api    -t ${aws_ecr_repository.api.repository_url}:${var.image_tag} .
    docker build -f deploy/docker/Dockerfile.worker -t ${aws_ecr_repository.worker.repository_url}:${var.image_tag} .

    docker push ${aws_ecr_repository.api.repository_url}:${var.image_tag}
    docker push ${aws_ecr_repository.worker.repository_url}:${var.image_tag}
  EOT
}

output "estimated_monthly_cost_usd" {
  description = "Infra only, excluding LLM tokens. Assumes ~100 runs and ~20k API requests."
  value = {
    lambda_api      = "~0.10"
    api_gateway     = "~0.02"
    step_functions  = "~0.08"
    fargate_spot    = "~0.10"
    dynamodb        = "~0.10"
    s3              = "~0.50"
    cloudwatch_logs = "~0.50"
    ecr             = "~0.10"
    total           = "~1.50  (no NAT gateway, no idle compute)"
    note            = "LLM tokens dominate: ~$0.30/run at gpt-4o-mini prices."
  }
}
