/**
 * Control plane (Lambda + API Gateway) and execution plane (ECS Fargate).
 */

data "aws_vpc" "selected" {
  id      = var.vpc_id != "" ? var.vpc_id : null
  default = var.vpc_id == "" ? true : null
}

data "aws_subnets" "selected" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.selected.id]
  }
}

locals {
  subnet_ids = length(var.subnet_ids) > 0 ? var.subnet_ids : data.aws_subnets.selected.ids

  # Shared runtime configuration. The API and the worker read the same state.
  forge_env = {
    FORGE_STORAGE          = "aws"
    FORGE_EXECUTION        = "stepfunctions"
    FORGE_S3_BUCKET        = aws_s3_bucket.artifacts.bucket
    FORGE_DDB_TABLE_PREFIX = local.name
    FORGE_ORCHESTRATOR     = "langgraph"
    # Step Functions owns durability across gates, so the in-task LangGraph
    # checkpointer is scratch only and must not try to open a SQLite file.
    FORGE_CHECKPOINTER = "memory"
    FORGE_VAR_ROOT     = "/tmp/forge"
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# Control plane — Lambda behind an HTTP API
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.name}-api"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "api" {
  function_name = "${local.name}-api"
  role          = aws_iam_role.api.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"
  memory_size   = var.api_memory_mb
  timeout       = var.api_timeout_s
  architectures = ["x86_64"]

  environment {
    variables = merge(local.forge_env, {
      FORGE_STATE_MACHINE_ARN = aws_sfn_state_machine.workflow.arn
      FORGE_API_TOKEN_PARAM   = aws_ssm_parameter.api_token.name
      FORGE_LLM_KEY_PARAM     = aws_ssm_parameter.llm_api_key.name
    })
  }

  depends_on = [aws_cloudwatch_log_group.api]
}

resource "aws_apigatewayv2_api" "studio" {
  name          = "${local.name}-studio"
  protocol_type = "HTTP"
  # The Studio UI is served by the same app, so the API is the only origin.
  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "DELETE", "OPTIONS"]
    allow_headers = ["authorization", "content-type", "x-forge-token"]
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.studio.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = var.api_timeout_s * 1000
}

resource "aws_apigatewayv2_route" "proxy" {
  api_id    = aws_apigatewayv2_api.studio.id
  route_key = "ANY /{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "root" {
  api_id    = aws_apigatewayv2_api.studio.id
  route_key = "ANY /"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.studio.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw.arn
    format = jsonencode({
      requestId = "$context.requestId"
      ip        = "$context.identity.sourceIp"
      route     = "$context.routeKey"
      status    = "$context.status"
      latency   = "$context.responseLatency"
    })
  }

  default_route_settings {
    # A cheap ceiling: the control plane is an internal tool, and this bounds
    # both accidental loops and the Lambda bill.
    throttling_burst_limit = 50
    throttling_rate_limit  = 25
  }
}

resource "aws_cloudwatch_log_group" "apigw" {
  name              = "/aws/apigateway/${local.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.studio.execution_arn}/*/*"
}

# ─────────────────────────────────────────────────────────────────────────────
# Execution plane — ECS Fargate, one task per workflow segment
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_ecs_cluster" "workers" {
  name = "${local.name}-workers"

  setting {
    name  = "containerInsights"
    value = var.environment == "prod" ? "enabled" : "disabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "workers" {
  cluster_name       = aws_ecs_cluster.workers.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = var.use_fargate_spot ? "FARGATE_SPOT" : "FARGATE"
    weight            = 1
  }
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.name}-worker"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory_mb
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.worker.arn

  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = "${aws_ecr_repository.worker.repository_url}:${var.image_tag}"
      essential = true
      environment = [
        for k, v in local.forge_env : { name = k, value = tostring(v) }
      ]
      secrets = [
        {
          name      = "FORGE_LLM_API_KEY"
          valueFrom = aws_ssm_parameter.llm_api_key.arn
        }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.worker.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "worker"
        }
      }
    }
  ])
}

resource "aws_security_group" "worker" {
  name        = "${local.name}-worker"
  description = "Forge execution tasks: egress only"
  vpc_id      = data.aws_vpc.selected.id

  egress {
    description = "Outbound to ECR, S3, DynamoDB, Step Functions and the LLM API"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # No ingress rules: nothing ever connects to a worker.
}
