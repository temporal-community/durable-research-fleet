data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  # AgentCore names are [a-zA-Z][a-zA-Z0-9_]{0,47}.
  runtime_name = "${replace(var.prefix, "-", "_")}_worker"
  endpoint_names = {
    for b in setunion([var.build_id], var.retained_build_ids) :
    b => "b_${replace(b, "/[^A-Za-z0-9]/", "_")}"
  }
  current_endpoint = local.endpoint_names[var.build_id]
  # Two Build IDs can sanitize to one endpoint name ("1.0" and "1_0" both -> b_1_0).
  endpoint_names_unique = length(distinct(values(local.endpoint_names))) == length(local.endpoint_names)

  secret_names = {
    TEMPORAL_API_KEY  = "${var.prefix}/agentcore/temporal-api-key"
    GEMINI_API_KEY    = "${var.prefix}/agentcore/gemini-api-key"
    ANTHROPIC_API_KEY = "${var.prefix}/agentcore/anthropic-api-key"
  }
}

# --- Secrets ----------------------------------------------------------------------------
#
# Created EMPTY. The values never enter Terraform state: `make ac-secrets` seeds
# them from .env. The Runtime holds only the ARNs and resolves them with its execution
# role at session start, so no key is readable from the Runtime's configuration.

resource "aws_secretsmanager_secret" "keys" {
  for_each = local.secret_names

  name                    = each.value
  description             = "${each.key} for the research-fleet AgentCore Worker"
  recovery_window_in_days = var.secret_recovery_window_days
}

# --- Code -------------------------------------------------------------------------------

resource "aws_s3_bucket" "code" {
  bucket        = "${var.prefix}-agentcore-code-${local.account_id}-${var.region}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "code" {
  bucket                  = aws_s3_bucket.code.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "code" {
  bucket = aws_s3_bucket.code.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "code" {
  bucket = aws_s3_bucket.code.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# The key carries the content hash: identical code is the same object and therefore
# the same Runtime configuration, so re-applying never publishes a spurious version.
resource "aws_s3_object" "code" {
  bucket = aws_s3_bucket.code.id
  key    = "code/${filemd5("${path.module}/${var.code_zip}")}.zip"
  source = "${path.module}/${var.code_zip}"
  etag   = filemd5("${path.module}/${var.code_zip}")
}

# --- Runtime ----------------------------------------------------------------------------

resource "aws_bedrockagentcore_agent_runtime" "worker" {
  agent_runtime_name = local.runtime_name
  description        = "Temporal Serverless Worker for research-fleet (build ${var.build_id})"
  role_arn           = aws_iam_role.runtime.arn

  agent_runtime_artifact {
    code_configuration {
      runtime     = "PYTHON_3_12"
      entry_point = ["worker_agentcore.py"]
      code {
        s3 {
          bucket = aws_s3_bucket.code.id
          prefix = aws_s3_object.code.key
          # Pin the exact object VERSION. Replacing the zip on the next build deletes the
          # key (a delete marker, in a versioned bucket); the version this Runtime
          # version was published from must stay reachable for every retained Build ID.
          version_id = aws_s3_object.code.version_id
        }
      }
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  lifecycle_configuration {
    idle_runtime_session_timeout = var.idle_runtime_session_timeout
    max_lifetime                 = var.max_lifetime
  }

  environment_variables = {
    TEMPORAL_ADDRESS           = var.temporal_address
    TEMPORAL_NAMESPACE         = var.temporal_namespace
    TEMPORAL_TASK_QUEUE        = var.task_queue
    TEMPORAL_DEPLOYMENT_NAME   = var.deployment_name
    BUILD_ID                   = var.build_id
    TEMPORAL_TLS               = "true"
    MAX_CONCURRENT_ACTIVITIES  = tostring(var.max_concurrent_activities)
    GRACEFUL_SHUTDOWN_SECONDS  = tostring(var.graceful_shutdown_seconds)
    AGENTCORE_DEBOUNCE_SECONDS = tostring(var.debounce_seconds)
    GEMINI_MODEL               = var.gemini_model
    ANTHROPIC_MODEL            = var.anthropic_model
    ANTHROPIC_RESEARCH_MODEL   = var.anthropic_research_model

    TEMPORAL_API_KEY_SECRET_ARN  = aws_secretsmanager_secret.keys["TEMPORAL_API_KEY"].arn
    GEMINI_API_KEY_SECRET_ARN    = aws_secretsmanager_secret.keys["GEMINI_API_KEY"].arn
    ANTHROPIC_API_KEY_SECRET_ARN = aws_secretsmanager_secret.keys["ANTHROPIC_API_KEY"].arn
  }

  tags = {
    build_id = var.build_id
  }
}

# One endpoint per Build ID. `ignore_changes` is the versioning guarantee: when the
# Runtime publishes a new version, an EXISTING endpoint must stay on the version it was
# created for, or every Workflow pinned to that Build ID would silently move to new code.
resource "aws_bedrockagentcore_agent_runtime_endpoint" "build" {
  for_each = local.endpoint_names

  name                  = each.value
  agent_runtime_id      = aws_bedrockagentcore_agent_runtime.worker.agent_runtime_id
  agent_runtime_version = aws_bedrockagentcore_agent_runtime.worker.agent_runtime_version
  description           = "Build ${each.key}, invoked by Temporal Cloud"

  lifecycle {
    ignore_changes = [agent_runtime_version]

    precondition {
      condition     = local.endpoint_names_unique
      error_message = "Two Build IDs map to the same endpoint name (non-alphanumerics become '_'). Rename one."
    }
  }
}
