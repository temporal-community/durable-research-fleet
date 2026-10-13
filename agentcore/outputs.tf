output "runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.worker.agent_runtime_arn
}

output "endpoint_arn" {
  description = "ARN of the endpoint for var.build_id. This, not the Runtime ARN, goes to Temporal."
  value       = aws_bedrockagentcore_agent_runtime_endpoint.build[var.build_id].agent_runtime_endpoint_arn
}

output "invoker_role_arn" {
  value = aws_iam_role.invoker.arn
}

output "external_id" {
  value = random_id.external_id.id
}

output "secret_arns" {
  value = { for k, s in aws_secretsmanager_secret.keys : k => s.arn }
}

output "register" {
  description = "What `make ac-register` runs; printed for transparency."
  value       = <<-E
    temporal worker deployment create-version \
      --deployment-name ${var.deployment_name} \
      --build-id ${var.build_id} \
      --aws-agentcore-endpoint-arn ${aws_bedrockagentcore_agent_runtime_endpoint.build[var.build_id].agent_runtime_endpoint_arn} \
      --aws-agentcore-assume-role-arn ${aws_iam_role.invoker.arn} \
      --aws-agentcore-assume-role-external-id ${random_id.external_id.id}
  E
}

output "temporal_namespace" {
  description = "The one Namespace this stack serves; scripts refuse to run against any other."
  value       = var.temporal_namespace
}

output "deployment_name" {
  value = var.deployment_name
}

output "build_id" {
  value = var.build_id
}

output "region" {
  value = var.region
}

output "code_md5" {
  description = "md5 of the zip in the applied state; ac-guard compares it to the new build."
  value       = aws_s3_object.code.etag
}
