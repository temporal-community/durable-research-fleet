# --- The Runtime's EXECUTION role: what the Worker itself may do ------------------------

data "aws_iam_policy_document" "runtime_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:*"]
    }
  }
}

resource "aws_iam_role" "runtime" {
  name               = "${var.prefix}-agentcore-runtime"
  description        = "Execution role for the research-fleet AgentCore Worker"
  assume_role_policy = data.aws_iam_policy_document.runtime_trust.json
}

data "aws_iam_policy_document" "runtime" {
  statement {
    sid       = "ReadOwnSecretsOnly"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [for s in aws_secretsmanager_secret.keys : s.arn]
  }

  statement {
    sid       = "ReadOwnCode"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.code.arn}/*"]
  }

  statement {
    sid = "Logs"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"]
  }

  statement {
    sid       = "LogDiscovery"
    actions   = ["logs:DescribeLogStreams", "logs:DescribeLogGroups"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:*"]
  }

  statement {
    sid = "Tracing"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["bedrock-agentcore"]
    }
  }

  statement {
    sid = "WorkloadIdentity"
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
      "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default/workload-identity/${local.runtime_name}-*",
    ]
  }
}

resource "aws_iam_role_policy" "runtime" {
  name   = "runtime"
  role   = aws_iam_role.runtime.id
  policy = data.aws_iam_policy_document.runtime.json
}

# --- The INVOCATION role: what TEMPORAL CLOUD may do to us ------------------------------
#
# Temporal's Worker Controller Instance assumes this role to call InvokeAgentRuntime.
# Two controls make that safe on a role that trusts another AWS account:
#   1. the ExternalId, so only a caller that knows it can assume the role at all;
#   2. the policy, which allows exactly two actions on exactly this Runtime.

resource "random_id" "external_id" {
  byte_length = 12
  prefix      = "${var.prefix}-"
}

data "aws_iam_policy_document" "invoker_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = var.temporal_invoker_principals
    }
    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [random_id.external_id.id]
    }
  }
}

resource "aws_iam_role" "invoker" {
  name                 = "Temporal-Cloud-Serverless-Worker-${var.prefix}"
  description          = "Assumed by Temporal Cloud to invoke the research-fleet AgentCore Runtime"
  assume_role_policy   = data.aws_iam_policy_document.invoker_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "invoker" {
  statement {
    sid     = "InvokeThisRuntimeOnly"
    actions = ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:GetAgentRuntimeEndpoint"]
    # The trailing wildcard covers the endpoint ARNs, which extend the Runtime ARN.
    resources = ["${aws_bedrockagentcore_agent_runtime.worker.agent_runtime_arn}*"]
  }
}

resource "aws_iam_role_policy" "invoker" {
  name   = "invoke-runtime"
  role   = aws_iam_role.invoker.id
  policy = data.aws_iam_policy_document.invoker.json
}
