# Plan-only tests for the wiring that fails SILENTLY when wrong.
#
#   cd terraform && terraform test
#
# Every run block uses `command = plan`, so nothing is created and these are safe
# and fast. They exist because each assertion below corresponds to a real bug this
# stack has already had.

# Every run is a plan-time wiring assertion. Mock Google so the suite needs no
# Application Default Credentials and can never mutate a real project.
mock_provider "google" {}

override_resource {
  target = google_service_account.worker_rt
  values = {
    account_id = "research-fleet-worker-rt"
    email      = "research-fleet-worker-rt@serverless-workers-demo.iam.gserviceaccount.com"
    name       = "projects/serverless-workers-demo/serviceAccounts/research-fleet-worker-rt@serverless-workers-demo.iam.gserviceaccount.com"
  }
  override_during = plan
}

variables {
  project_id      = "serverless-workers-demo"
  region          = "us-central1"
  zone            = "us-central1-a"
  name_prefix     = "research-fleet"
  build_id        = "v1"
  deployment_name = "research-fleet"
  # The plan validates that a path exists; tests do not execute or upload it.
  temporal_cli_binary = "../Makefile"
  # Keep plan-only tests hermetic: production may auto-detect the operator's IP,
  # but tests must not disclose it to api.ipify.org.
  ssh_source_ranges = ["127.0.0.1/32"]
}

run "worker_pool_env_contract" {
  command = plan

  # The bug this catches: worker registers under a different Worker Deployment
  # than the one the compute config is attached to, and Workflows hang silently.
  assert {
    condition = length([
      for e in google_cloud_run_v2_worker_pool.fleet.template[0].containers[0].env :
      e if e.name == "TEMPORAL_DEPLOYMENT_NAME" && e.value == var.deployment_name
    ]) == 1
    error_message = "pool must pass TEMPORAL_DEPLOYMENT_NAME matching var.deployment_name"
  }

  # The bug this catches: SDK default of 100 activity slots means one instance
  # absorbs a whole burst, no backlog forms, and the WCI never scales.
  assert {
    condition = length([
      for e in google_cloud_run_v2_worker_pool.fleet.template[0].containers[0].env :
      e if e.name == "MAX_CONCURRENT_ACTIVITIES" && e.value == "1"
    ]) == 1
    error_message = "pool must set MAX_CONCURRENT_ACTIVITIES=1 (KB §4, and required for backlog to form)"
  }

  # The bug this catches: worker defaults to TLS, self-hosted frontend is
  # plaintext gRPC, handshake fails with an opaque transport error.
  assert {
    condition = length([
      for e in google_cloud_run_v2_worker_pool.fleet.template[0].containers[0].env :
      e if e.name == "TEMPORAL_TLS" && e.value == "false"
    ]) == 1
    error_message = "pool must set TEMPORAL_TLS=false for the plaintext self-hosted frontend"
  }

  assert {
    condition = length([
      for e in google_cloud_run_v2_worker_pool.fleet.template[0].containers[0].env :
      e if e.name == "TEMPORAL_ADDRESS" && e.value == "${var.temporal_internal_ip}:7233"
    ]) == 1
    error_message = "pool must dial the VM's static internal IP"
  }

  assert {
    condition = length([
      for e in google_cloud_run_v2_worker_pool.fleet.template[0].containers[0].env :
      e if e.name == "GRACEFUL_SHUTDOWN_SECONDS"
    ]) == 1
    error_message = "pool must set GRACEFUL_SHUTDOWN_SECONDS so scale-in can drain (KB §7.3)"
  }

  assert {
    condition = length([
      for e in google_cloud_run_v2_worker_pool.fleet.template[0].containers[0].env :
      e if e.name == "GEMINI_MODEL" && e.value == var.gemini_model
    ]) == 1
    error_message = "pool must pass the configured Gemini model to the Worker"
  }

  assert {
    condition = length([
      for e in google_cloud_run_v2_worker_pool.fleet.template[0].containers[0].env :
      e if e.name == "ANTHROPIC_MODEL" && e.value == var.anthropic_model
    ]) == 1
    error_message = "pool must pass the configured Claude model to the Worker"
  }
}

run "worker_pool_starts_empty_and_stays_private" {
  command = plan

  assert {
    condition     = google_cloud_run_v2_worker_pool.fleet.scaling[0].manual_instance_count == 0
    error_message = "pool must start at 0 instances — the WCI owns the count"
  }

  assert {
    condition     = google_cloud_run_v2_worker_pool.fleet.scaling[0].scaling_mode == "MANUAL"
    error_message = "Worker Pools use manual scaling; the WCI drives the count"
  }

  # Direct VPC egress is what removes the need for a public tunnel.
  assert {
    condition     = google_cloud_run_v2_worker_pool.fleet.template[0].vpc_access[0].egress == "PRIVATE_RANGES_ONLY"
    error_message = "pool must use Direct VPC egress to reach Temporal privately"
  }
}

run "iam_least_privilege_and_the_actas_binding" {
  command = plan

  # THE binding that made scale-from-zero work. Absent it, UpdateWorkerSetSize
  # fails on every attempt and the pool silently never scales — with no error
  # anywhere in GCP. Neither KB §7.6 nor the upstream module grants it.
  assert {
    condition     = google_service_account_iam_member.invoker_acts_as_worker_rt.role == "roles/iam.serviceAccountUser"
    error_message = "invoker needs iam.serviceAccounts.actAs on the pool's RUNTIME service account"
  }

  assert {
    condition     = google_service_account_iam_member.vm_impersonates_invoker.role == "roles/iam.serviceAccountTokenCreator"
    error_message = "the Temporal VM must be able to impersonate the invoker"
  }

  # The docs ask for run.developer "or equivalent". We grant the equivalent: the
  # predefined role is project-wide and would also let the invoker delete the public
  # web Service. Assert the custom role carries exactly the two verbs the WCI calls —
  # if scaling breaks, this is the first place to look.
  assert {
    condition = toset(google_project_iam_custom_role.worker_pool_scaler.permissions) == toset([
      "run.workerpools.get",
      "run.workerpools.update",
    ])
    error_message = "invoker needs exactly run.workerpools.get + update — no more, no less"
  }

  # The binding's `role` is the custom role's id, which is only known after apply, so
  # assert on the role_id literal instead — this is a plan-only test suite.
  assert {
    condition     = google_project_iam_custom_role.worker_pool_scaler.role_id == "workerPoolScaler"
    error_message = "the invoker must be bound to the custom scaler role, not roles/run.developer"
  }

  # Three distinct identities: the thing that can scale must not be able to run.
  assert {
    condition = length(distinct([
      google_service_account.invoker.account_id,
      google_service_account.temporal_vm.account_id,
      google_service_account.worker_rt.account_id,
    ])) == 3
    error_message = "invoker / temporal-vm / worker-rt must be separate identities"
  }
}

run "temporal_frontend_is_never_public" {
  command = plan

  assert {
    condition     = google_compute_firewall.temporal_internal.source_ranges == toset([var.subnet_cidr])
    error_message = "port 7233 must be reachable only from inside the subnet, never 0.0.0.0/0"
  }

  assert {
    condition     = !contains(tolist(google_compute_firewall.temporal_internal.source_ranges), "0.0.0.0/0")
    error_message = "the Temporal frontend must not be exposed to the internet"
  }

  assert {
    condition     = !contains(tolist(google_compute_firewall.ssh[0].source_ranges), "0.0.0.0/0")
    error_message = "SSH must be scoped to the operator, not the whole internet"
  }

  # The three assertions above only inspect the rules THIS configuration declares,
  # which is exactly why they passed while the Web UI on :8233 was answering
  # unauthenticated on the VM's public IP (2026-07-31). An allow rule created outside
  # Terraform beat GCP's implied deny, and nothing here could see it.
  #
  # The default-deny is the structural fix, so assert its shape rather than trusting
  # it: it has to cover the whole internet, and it has to outrank anything created
  # later at the default priority of 1000.
  assert {
    condition     = google_compute_firewall.deny_public_to_vm.source_ranges == toset(["0.0.0.0/0"])
    error_message = "the default-deny must cover the whole internet, or it is not a default"
  }

  assert {
    condition     = google_compute_firewall.deny_public_to_vm.priority < 1000
    error_message = "a deny at priority >= 1000 is inert against an allow created at the default priority"
  }

  # Ordering is the whole design: the two intended allows sit above the deny.
  assert {
    condition = alltrue([
      google_compute_firewall.temporal_internal.priority < google_compute_firewall.deny_public_to_vm.priority,
      google_compute_firewall.ssh[0].priority < google_compute_firewall.deny_public_to_vm.priority,
    ])
    error_message = "the intended allows must outrank the default-deny, or the VM is unreachable"
  }
}

run "worker_controller_dynamic_config" {
  command = plan

  assert {
    condition     = local.wci_dynamic_config["workercontroller.enabled"] == "true"
    error_message = "the Worker Controller must be enabled"
  }

  # Cloud Run needs BOTH. The published self-hosted docs list only no-sync (the
  # Lambda setup); without rate-based, create-version fails with
  # "Could not instantiate scaling algorithm with type 'rate-based'".
  assert {
    condition = alltrue([
      strcontains(local.wci_dynamic_config["workercontroller.scaling_algorithms.enabled"], "no-sync"),
      strcontains(local.wci_dynamic_config["workercontroller.scaling_algorithms.enabled"], "rate-based"),
    ])
    error_message = "scaling_algorithms must enable BOTH no-sync and rate-based for Cloud Run"
  }

  assert {
    condition     = strcontains(local.wci_dynamic_config["workercontroller.compute_providers.enabled"], "gcp-cloud-run")
    error_message = "gcp-cloud-run must be an enabled compute provider"
  }

  # The flags must actually reach the server. start-dev has no
  # --dynamic-config-file, so a YAML file would be silently ignored.
  assert {
    condition     = strcontains(local.wci_flags, "--dynamic-config-value")
    error_message = "dynamic config must be rendered into launch flags"
  }

  assert {
    condition     = length(regexall("--dynamic-config-value", local.wci_flags)) == length(local.wci_dynamic_config)
    error_message = "every dynamic config entry must become a launch flag"
  }
}

run "the_web_tier_is_a_service_not_a_worker_pool" {
  command = plan

  # Pin web_public to the committed default. `terraform test` auto-loads
  # terraform.tfvars, so an operator who sets `web_public = true` locally for a live
  # demo would otherwise turn the two exposure assertions below green-by-vacuum —
  # the suite would stop guarding the posture precisely while it is open. What these
  # assertions defend is what the repo ships, which is independent of any local
  # override, so state that here rather than reading it from the environment.
  variables {
    web_public = false
  }

  # THE thing to get wrong. The web tier is request-serving and is an ordinary
  # Temporal CLIENT; the Worker Pool is long-lived pollers scaled by the WCI. They
  # are different Cloud Run products.
  #
  # This assertion is the one that matters most: without the command override the
  # shared image starts its DEFAULT entrypoint, which is the Worker. The web
  # Service would then quietly poll the Task Queue, absorb the burst itself, and
  # destroy the scaling demo — while looking completely healthy.
  assert {
    condition     = google_cloud_run_v2_service.web.template[0].containers[0].command == tolist(["uvicorn"])
    error_message = "the web Service must override the container command, or it starts a second Worker and absorbs the burst"
  }

  assert {
    condition     = contains(google_cloud_run_v2_service.web.template[0].containers[0].args, "web:app")
    error_message = "the web Service must run web:app"
  }

  # Added 2026-07-31. `invoker_iam_disabled = true` is Google's documented answer to
  # domain-restricted sharing refusing an `allUsers` binding — and it produces the
  # same exposure the org policy exists to prevent. Both halves are asserted because
  # either one alone leaves the Service publicly invokable.
  assert {
    condition     = google_cloud_run_v2_service.web.invoker_iam_disabled == false
    error_message = "invoker_iam_disabled = true makes this Service callable by anyone with the URL"
  }

  assert {
    condition     = google_cloud_run_v2_service.web.ingress == "INGRESS_TRAFFIC_INTERNAL_ONLY"
    error_message = "the web Service must not be internet-reachable; the presenter runs the console locally"
  }

  # The service name and region are both in this public repo, and Cloud Run's newer
  # URL form is derivable from them, so "nobody can guess the hostname" is not a
  # control. Access is named in-domain identities only, and none by default.
  assert {
    condition     = !contains(var.web_invoker_users, "allUsers") && !contains(var.web_invoker_users, "allAuthenticatedUsers")
    error_message = "web_invoker_users is for named in-domain users; DRS rejects the special principals anyway"
  }

  # Same image as the pool: one build, one push, no version drift between tiers.
  assert {
    condition     = google_cloud_run_v2_service.web.template[0].containers[0].image == google_cloud_run_v2_worker_pool.fleet.template[0].containers[0].image
    error_message = "the web Service and the Worker Pool must run the SAME image"
  }

  # It talks to Temporal on the private address, same as the pool.
  assert {
    condition     = google_cloud_run_v2_service.web.template[0].vpc_access[0].egress == "PRIVATE_RANGES_ONLY"
    error_message = "the web tier needs Direct VPC egress to reach the Temporal frontend"
  }

  # Distinct runtime identity from the Worker, so the public tier holds no more
  # privilege than serving pages requires.
  assert {
    condition     = google_service_account.web_rt.account_id != google_service_account.worker_rt.account_id
    error_message = "the web tier must run as its own service account"
  }

  # Teardown must be reliable — the provider default of true fails destroy partway.
  assert {
    condition     = google_cloud_run_v2_service.web.deletion_protection == false
    error_message = "deletion_protection must be false or `make down` breaks halfway"
  }
}

run "provider_keys_never_land_in_state" {
  command = plan

  # Injected by reference, not by value. A `sensitive` variable would still be
  # written to terraform.tfstate in the clear — and state on this repo has already
  # been a real exposure.
  assert {
    condition = length([
      for e in google_cloud_run_v2_worker_pool.fleet.template[0].containers[0].env :
      e if e.name == "GEMINI_API_KEY" && length(e.value_source) == 1
    ]) == 1
    error_message = "GEMINI_API_KEY must come from a secret_key_ref, never a plaintext value"
  }

  assert {
    condition = length([
      for e in google_cloud_run_v2_worker_pool.fleet.template[0].containers[0].env :
      e if e.name == "ANTHROPIC_API_KEY" && length(e.value_source) == 1
    ]) == 1
    error_message = "ANTHROPIC_API_KEY must come from a secret_key_ref, never a plaintext value"
  }

  # No key supplied by default, so a clean apply works and the hello app (and
  # `make verify SCALE=1`) keeps passing without any Gemini credentials.
  assert {
    condition     = var.gemini_api_key == null
    error_message = "gemini_api_key must default to null; populate the secret out of band"
  }


  assert {
    condition     = var.anthropic_api_key == null
    error_message = "anthropic_api_key must default to null; populate the secret out of band"
  }

  # Only the Worker reads it. The web tier never calls Gemini.
  assert {
    condition     = google_secret_manager_secret_iam_member.worker_reads_gemini.member == "serviceAccount:${google_service_account.worker_rt.email}"
    error_message = "only the Worker runtime SA should be able to read the Gemini key"
  }


  assert {
    condition     = google_secret_manager_secret_iam_member.worker_reads_anthropic.member == "serviceAccount:${google_service_account.worker_rt.email}"
    error_message = "only the Worker runtime SA should be able to read the Anthropic key"
  }
}

run "spend_on_a_public_page_is_bounded" {
  command = plan

  # The public phone page can start Workflows that cost money. maxInstances is the
  # control that actually bounds it — the rate limit and length cap in web.py are
  # per-instance and best-effort.
  assert {
    condition     = var.max_instances <= 50
    error_message = "max_instances is the real spend ceiling on a public demo; keep it sane"
  }

  assert {
    condition     = google_cloud_run_v2_service.web.template[0].scaling[0].max_instance_count <= 10
    error_message = "the web tier serves a room, not the internet"
  }
}

run "required_apis_are_enabled" {
  command = plan

  # Each of these has produced a real half-built apply or a misleading error.
  assert {
    condition = alltrue([
      for api in [
        "compute.googleapis.com",
        "run.googleapis.com",
        "artifactregistry.googleapis.com",
        "storage.googleapis.com",
        "iamcredentials.googleapis.com", # else: getAccessToken denied
        "iam.googleapis.com",            # else: SERVICE_DISABLED creating SAs
        "cloudresourcemanager.googleapis.com",
        "secretmanager.googleapis.com", # else: the pool can't read the Gemini key
      ] : contains(keys(google_project_service.apis), api)
    ])
    error_message = "a required API is missing from the enablement list"
  }

  # Disabling APIs on destroy can break unrelated things in a shared project.
  assert {
    condition     = alltrue([for s in google_project_service.apis : s.disable_on_destroy == false])
    error_message = "APIs must not be disabled on destroy"
  }
}

run "adoption_is_off_by_default" {
  command = plan

  # The bug this catches: unconditional import blocks make `apply` impossible on
  # any project where those resources don't already exist.
  assert {
    condition     = var.adopt_existing == false
    error_message = "adopt_existing must default to false so a clean project can apply"
  }
}
