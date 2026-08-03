/**
 * The workflow state machine.
 *
 *   RunSegment (Fargate, .sync)
 *        │
 *        ├─ status = PAUSED  → RegisterApprovalToken (.waitForTaskToken)
 *        │                          │  parks, free, until POST /approve
 *        │                          └─ SendTaskSuccess → RunSegment (resume)
 *        │
 *        └─ otherwise        → Succeeded | Failed
 *
 * The token wait is the whole reason this is a Standard workflow: it can hold for
 * up to a year at no cost, which is the only primitive that matches a workflow
 * paused at `approval.coding` overnight. Express workflows cap at five minutes
 * and do not support task tokens.
 */

resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/vendedlogs/states/${local.name}"
  retention_in_days = var.log_retention_days
}

# Persists the task token so the API can release the gate later.
resource "aws_lambda_function" "register_token" {
  function_name = "${local.name}-register-token"
  role          = aws_iam_role.register_token.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"
  memory_size   = 256
  timeout       = 30

  image_config {
    command = ["forge.aws_lambda.register_token_handler"]
  }

  environment {
    variables = local.forge_env
  }
}

resource "aws_sfn_state_machine" "workflow" {
  name     = "${local.name}-workflow"
  role_arn = aws_iam_role.sfn.arn
  type     = "STANDARD"

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = false # workflow payloads can contain requirement text
    level                  = "ERROR"
  }

  tracing_configuration {
    enabled = var.environment == "prod"
  }

  definition = jsonencode({
    Comment = "Forge SDLC workflow — segments separated by human approval gates"
    StartAt = "RunSegment"
    States = {
      RunSegment = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          Cluster        = aws_ecs_cluster.workers.arn
          TaskDefinition = aws_ecs_task_definition.worker.arn
          LaunchType     = var.use_fargate_spot ? null : "FARGATE"
          CapacityProviderStrategy = var.use_fargate_spot ? [
            { CapacityProvider = "FARGATE_SPOT", Weight = 1 }
          ] : null
          NetworkConfiguration = {
            AwsvpcConfiguration = {
              Subnets = local.subnet_ids
              # Public IP instead of a NAT gateway: egress-only security group,
              # and NAT would cost more than everything else combined.
              AssignPublicIp = "ENABLED"
              SecurityGroups = [aws_security_group.worker.id]
            }
          }
          Overrides = {
            ContainerOverrides = [
              {
                Name        = "worker"
                "Command.$" = "States.Array('--workflow-id', $.workflow_id, '--workers', '${var.worker_max_workers}')"
              }
            ]
          }
        }
        TimeoutSeconds = var.segment_timeout_s
        ResultPath     = "$.segment"
        Retry = [
          {
            # Spot reclaim or a transient task failure: the engine resumes from
            # its last checkpoint, so retrying a segment is safe.
            ErrorEquals     = ["States.TaskFailed", "ECS.AmazonECSException"]
            IntervalSeconds = 10
            MaxAttempts     = 2
            BackoffRate     = 2.0
          },
          {
            ErrorEquals     = ["States.ALL"]
            IntervalSeconds = 5
            MaxAttempts     = 1
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            ResultPath  = "$.error"
            Next        = "Failed"
          }
        ]
        Next = "CheckWorkflowState"
      }

      CheckWorkflowState = {
        Type     = "Task"
        Resource = aws_lambda_function.register_token.arn
        Parameters = {
          "action"        = "read_state"
          "workflow_id.$" = "$.workflow_id"
        }
        ResultPath = "$.state"
        Next       = "PausedForHuman?"
      }

      "PausedForHuman?" = {
        Type = "Choice"
        Choices = [
          {
            Variable     = "$.state.status"
            StringEquals = "PAUSED"
            Next         = "AwaitApproval"
          },
          {
            Variable     = "$.state.status"
            StringEquals = "FAILED"
            Next         = "Failed"
          }
        ]
        Default = "Succeeded"
      }

      AwaitApproval = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke.waitForTaskToken"
        Parameters = {
          FunctionName = aws_lambda_function.register_token.arn
          Payload = {
            "action"        = "register"
            "workflow_id.$" = "$.workflow_id"
            "gate.$"        = "$.state.gate"
            "task_token.$"  = "$$.Task.Token"
          }
        }
        # Costs nothing while parked. Beyond this the run is abandoned rather
        # than left waiting forever.
        TimeoutSeconds = var.approval_timeout_s
        ResultPath     = "$.decision"
        Catch = [
          {
            ErrorEquals = ["States.Timeout"]
            ResultPath  = "$.error"
            Next        = "Abandoned"
          }
        ]
        Next = "ResumeSegment"
      }

      ResumeSegment = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          Cluster        = aws_ecs_cluster.workers.arn
          TaskDefinition = aws_ecs_task_definition.worker.arn
          LaunchType     = var.use_fargate_spot ? null : "FARGATE"
          CapacityProviderStrategy = var.use_fargate_spot ? [
            { CapacityProvider = "FARGATE_SPOT", Weight = 1 }
          ] : null
          NetworkConfiguration = {
            AwsvpcConfiguration = {
              Subnets        = local.subnet_ids
              AssignPublicIp = "ENABLED"
              SecurityGroups = [aws_security_group.worker.id]
            }
          }
          Overrides = {
            ContainerOverrides = [
              {
                Name        = "worker"
                "Command.$" = "States.Array('--workflow-id', $.workflow_id, '--decision', $.decision.decision, '--rationale', $.decision.rationale, '--workers', '${var.worker_max_workers}')"
              }
            ]
          }
        }
        TimeoutSeconds = var.segment_timeout_s
        ResultPath     = "$.segment"
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            ResultPath  = "$.error"
            Next        = "Failed"
          }
        ]
        # Loop: a run passes through as many gates as its playbook defines.
        Next = "CheckWorkflowState"
      }

      Succeeded = { Type = "Succeed" }
      Abandoned = {
        Type  = "Fail"
        Error = "ApprovalTimeout"
        Cause = "No human decision within the approval timeout"
      }
      Failed = {
        Type  = "Fail"
        Error = "WorkflowFailed"
        Cause = "A workflow segment failed; see the audit trace for the gate and node"
      }
    }
  })
}
