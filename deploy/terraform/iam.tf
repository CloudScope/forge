/**
 * Least-privilege roles. Four identities, each scoped to what it actually does:
 *
 *   api             read/write state, start executions, release task tokens
 *   worker          read/write state (the engine's own persistence)
 *   register_token  read one workflow, write one token
 *   sfn             run tasks, invoke the token lambda
 *   task_execution  pull the image, write logs (AWS-managed policy)
 */

data "aws_iam_policy_document" "assume_lambda" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "assume_ecs_task" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "assume_sfn" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

locals {
  table_arns = concat(
    [for t in aws_dynamodb_table.documents : t.arn],
    [aws_dynamodb_table.events.arn],
  )
}

# Shared: full read/write on Forge state (the engine persists continuously).
data "aws_iam_policy_document" "state_access" {
  statement {
    sid = "ObjectState"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]
  }

  statement {
    sid = "DocumentState"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:BatchWriteItem",
    ]
    resources = local.table_arns
  }

  statement {
    sid       = "ReadSecrets"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [aws_ssm_parameter.llm_api_key.arn, aws_ssm_parameter.api_token.arn]
  }
}

resource "aws_iam_policy" "state_access" {
  name   = "${local.name}-state-access"
  policy = data.aws_iam_policy_document.state_access.json
}

# ── API ──────────────────────────────────────────────────────────────────────

resource "aws_iam_role" "api" {
  name               = "${local.name}-api"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_iam_role_policy_attachment" "api_state" {
  role       = aws_iam_role.api.name
  policy_arn = aws_iam_policy.state_access.arn
}

resource "aws_iam_role_policy_attachment" "api_logs" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "api_orchestration" {
  statement {
    sid       = "StartRuns"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.workflow.arn]
  }
  statement {
    sid = "ReleaseGates"
    # Task tokens are not addressable as resources; the action itself is the grant.
    actions   = ["states:SendTaskSuccess", "states:SendTaskFailure"]
    resources = ["*"]
  }
  statement {
    sid       = "InspectRuns"
    actions   = ["states:DescribeExecution", "states:ListExecutions"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "api_orchestration" {
  name   = "orchestration"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api_orchestration.json
}

# ── Worker ───────────────────────────────────────────────────────────────────

resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.assume_ecs_task.json
}

resource "aws_iam_role_policy_attachment" "worker_state" {
  role       = aws_iam_role.worker.name
  policy_arn = aws_iam_policy.state_access.arn
}

# The worker writes its own logs through the awslogs driver (execution role),
# and needs nothing else: it never starts executions or touches task tokens.

# ── Token lambda ─────────────────────────────────────────────────────────────

resource "aws_iam_role" "register_token" {
  name               = "${local.name}-register-token"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_iam_role_policy_attachment" "register_token_state" {
  role       = aws_iam_role.register_token.name
  policy_arn = aws_iam_policy.state_access.arn
}

resource "aws_iam_role_policy_attachment" "register_token_logs" {
  role       = aws_iam_role.register_token.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ── ECS task execution (image pull + log write) ───────────────────────────────

resource "aws_iam_role" "task_execution" {
  name               = "${local.name}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.assume_ecs_task.json
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "task_execution_secrets" {
  statement {
    actions   = ["ssm:GetParameters"]
    resources = [aws_ssm_parameter.llm_api_key.arn]
  }
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  name   = "secrets"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.task_execution_secrets.json
}

# ── Step Functions ───────────────────────────────────────────────────────────

resource "aws_iam_role" "sfn" {
  name               = "${local.name}-sfn"
  assume_role_policy = data.aws_iam_policy_document.assume_sfn.json
}

data "aws_iam_policy_document" "sfn" {
  statement {
    sid       = "RunSegments"
    actions   = ["ecs:RunTask", "ecs:StopTask", "ecs:DescribeTasks"]
    resources = ["*"]
  }

  statement {
    sid       = "PassTaskRoles"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.worker.arn, aws_iam_role.task_execution.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }

  statement {
    sid       = "SyncRunTaskCallbacks"
    actions   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
    resources = ["arn:aws:events:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:rule/StepFunctionsGetEventsForECSTaskRule"]
  }

  statement {
    sid       = "InvokeTokenLambda"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.register_token.arn]
  }

  statement {
    sid = "Logging"
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "sfn" {
  name   = "execution"
  role   = aws_iam_role.sfn.id
  policy = data.aws_iam_policy_document.sfn.json
}
