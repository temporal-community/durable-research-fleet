# Plan-only assertions on the AgentCore stack. Mocked provider: no AWS call is made.
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  # A mocked policy document would otherwise be random text, which the role rejects.
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}
mock_provider "random" {}

variables {
  temporal_namespace = "ns-under-test.a1b2c"
  temporal_address   = "ns-under-test.a1b2c.tmprl.cloud:7233"
  build_id           = "1.0.0"
  code_zip           = "tests/fixture.zip"
  # Pin everything a developer's gitignored terraform.tfvars could otherwise leak in.
  retained_build_ids = []
  debounce_seconds   = 60
}

run "the_worker_is_versioned_pinned_to_one_endpoint_per_build" {
  command = plan

  assert {
    condition     = length(aws_bedrockagentcore_agent_runtime_endpoint.build) == 1
    error_message = "exactly one endpoint per Build ID"
  }
  assert {
    condition     = aws_bedrockagentcore_agent_runtime.worker.environment_variables["BUILD_ID"] == "1.0.0"
    error_message = "the Runtime must carry the Build ID the Worker registers under"
  }
  assert {
    condition     = aws_bedrockagentcore_agent_runtime.worker.environment_variables["TEMPORAL_DEPLOYMENT_NAME"] == "research-fleet-agentcore"
    error_message = "deployment name must match what register.sh creates (AGENTS.md gate #6)"
  }
}

run "an_old_endpoint_never_follows_a_new_runtime_version" {
  command = plan

  variables {
    retained_build_ids = ["0.9.0"]
  }

  assert {
    condition     = length(aws_bedrockagentcore_agent_runtime_endpoint.build) == 2
    error_message = "a retained Build ID keeps its endpoint"
  }
}

run "activity_slots_on_agentcore" {
  command = plan

  # Deliberately NOT 1 here (AGENTS.md gate #5 is about Cloud Run). See the variable.
  assert {
    condition     = aws_bedrockagentcore_agent_runtime.worker.environment_variables["MAX_CONCURRENT_ACTIVITIES"] == "3"
    error_message = "AgentCore runs 3 Activities per session so the fan-out is not serialised"
  }
}

run "research_subquestions_use_the_faster_model" {
  command = plan

  assert {
    condition     = aws_bedrockagentcore_agent_runtime.worker.environment_variables["ANTHROPIC_RESEARCH_MODEL"] == "claude-sonnet-5-5"
    error_message = "sub-questions run on Sonnet; planning and synthesis stay on the main model"
  }
  assert {
    condition     = aws_bedrockagentcore_agent_runtime.worker.environment_variables["ANTHROPIC_MODEL"] == "claude-opus-5"
    error_message = "the main model must be unchanged"
  }
}

run "secrets_are_referenced_not_inlined" {
  command = plan

  assert {
    condition     = !contains(keys(aws_bedrockagentcore_agent_runtime.worker.environment_variables), "TEMPORAL_API_KEY")
    error_message = "the API key must never be a plain Runtime env var"
  }
  assert {
    condition     = !contains(keys(aws_bedrockagentcore_agent_runtime.worker.environment_variables), "GEMINI_API_KEY")
    error_message = "model keys must be Secrets Manager references"
  }
}

run "temporal_trust_requires_the_external_id" {
  command = plan

  assert {
    condition     = anytrue([for st in data.aws_iam_policy_document.invoker_trust.statement : anytrue([for c in st.condition : c.variable == "sts:ExternalId"])])
    error_message = "the cross-account role must be gated on the ExternalId"
  }
}

run "a_self_hosted_address_is_refused" {
  command = plan

  variables {
    temporal_address = "10.10.0.10:7233"
  }
  expect_failures = [var.temporal_address]
}

run "a_non_cloud_namespace_is_refused" {
  command = plan

  variables {
    temporal_namespace = "default"
  }
  expect_failures = [var.temporal_namespace]
}

run "colliding_build_ids_are_refused" {
  command = plan

  variables {
    retained_build_ids = ["1_0_0"]
  }
  expect_failures = [aws_bedrockagentcore_agent_runtime_endpoint.build]
}

run "a_zero_debounce_is_refused" {
  command = plan

  variables {
    debounce_seconds = 0
  }
  expect_failures = [var.debounce_seconds]
}

run "the_runtime_is_pinned_to_an_s3_object_version" {
  command = plan

  assert {
    condition     = aws_bedrockagentcore_agent_runtime.worker.agent_runtime_artifact[0].code_configuration[0].code[0].s3[0].prefix != ""
    error_message = "the Runtime must reference the zip it was built from"
  }
}
