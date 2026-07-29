# The worker pool. Starts at zero instances; the Worker Controller resizes it.
#
# Terraform owns the pool's shape (image, env, networking) but NOT its instance
# count at runtime — that is the WCI's job. The lifecycle block below stops
# Terraform from fighting the WCI and reverting it to 0 on every plan.
resource "google_cloud_run_v2_worker_pool" "fleet" {
  name     = "${local.prefix}-worker-pool"
  location = var.region

  # launch_stage is deliberately unset. Setting "BETA" produces permanent drift:
  # the API reports "GA" (Worker Pools have graduated), so every plan wanted to
  # push GA -> BETA forever. Leaving it computed keeps plans clean.

  # Required for `make down` to work at all. The provider defaults this to true,
  # so `terraform destroy` fails with "cannot destroy WorkerPool without setting
  # deletion_protection=false and running `terraform apply`" — and worse, it
  # fails PARTWAY, after other resources are already gone, leaving a half-torn
  # stack that needs manual cleanup. This is a disposable demo stack; teardown
  # must be reliable. Flip it to true if you ever run something you care about.
  deletion_protection = false

  scaling {
    scaling_mode          = "MANUAL"
    manual_instance_count = 0
  }

  template {
    service_account = google_service_account.worker_rt.email

    containers {
      image = local.worker_image

      env {
        name = "TEMPORAL_ADDRESS"
        # Static internal IP, reached over Direct VPC egress. Never public.
        value = local.temporal_address
      }
      env {
        name  = "TEMPORAL_NAMESPACE"
        value = var.temporal_namespace
      }
      env {
        name  = "TEMPORAL_TASK_QUEUE"
        value = var.task_queue
      }
      env {
        # Must match the Worker Deployment the bootstrap attaches the compute
        # config to. If the worker registers under a different name, that
        # deployment has no current version and Workflows hang with no error.
        name  = "TEMPORAL_DEPLOYMENT_NAME"
        value = var.deployment_name
      }
      env {
        name  = "BUILD_ID"
        value = var.build_id
      }
      env {
        # Load-bearing. The self-hosted frontend is plaintext gRPC; the worker
        # defaults to TLS when it sees an API key and would fail the handshake.
        name  = "TEMPORAL_TLS"
        value = "false"
      }
      env {
        # 1 by default: KB §4 wants slot isolation, and the SDK default of 100
        # would let one instance swallow a whole burst so no backlog ever forms
        # for the WCI to scale on.
        name  = "MAX_CONCURRENT_ACTIVITIES"
        value = tostring(var.max_concurrent_activities)
      }
      env {
        name  = "GRACEFUL_SHUTDOWN_SECONDS"
        value = tostring(var.graceful_shutdown_seconds)
      }
      env {
        # Fan-out width. One Activity slot per instance, so this IS the number of
        # Serverless Workers one question lights up — the figure the room watches.
        name  = "MAX_SUBQUESTIONS"
        value = tostring(var.max_subquestions)
      }
      env {
        name  = "RESEARCH_EFFORT"
        value = var.research_effort
      }
      env {
        # The research app's Claude key, injected from Secret Manager rather than
        # a plaintext value so it never lands in Terraform state.
        #
        # The hello app does not read this, which is what keeps
        # `make verify SCALE=1` working on a stack with no key configured.
        #
        # ⚠️ CORRECTED 2026-07-29 on the first real deploy. The claim that used to
        # sit here — "if the secret has no version the container still starts and
        # only the research Activities fail" — is FALSE. Cloud Run resolves this
        # ref at POOL CREATE TIME and refuses outright:
        #
        #   Error code 9: ...secret_key_ref.name: Secret .../versions/latest
        #   was not found
        #
        # So a version must exist BEFORE the pool is created. `make secret` seeds
        # one (the real key, or a placeholder when ANTHROPIC_API_KEY is unset) and
        # `make up` runs it before this apply. Do not assume an empty secret is a
        # tolerable state — it makes the pool uncreatable.
        name = "ANTHROPIC_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.anthropic.secret_id
            version = "latest"
          }
        }
      }

      resources {
        limits = {
          cpu    = var.worker_cpu
          memory = var.worker_memory
        }
      }
    }

    vpc_access {
      # PRIVATE_RANGES_ONLY: RFC1918 traffic goes through the VPC (so it can
      # reach Temporal), everything else takes the normal internet path.
      egress = "PRIVATE_RANGES_ONLY"

      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.subnet.id
      }
    }
  }

  lifecycle {
    ignore_changes = [
      # Hand instance count over to the Worker Controller after creation.
      scaling[0].manual_instance_count,
      # Stamped by whoever last wrote to the pool. `make register-queues` uses
      # gcloud, and the WCI writes to it constantly, so Terraform would otherwise
      # report drift ("client = gcloud -> null") on every single plan.
      client,
      client_version,
    ]
  }

  depends_on = [
    google_project_service.apis,
    google_artifact_registry_repository_iam_member.worker_rt_reader,
    # Without the accessor binding first, the pool's containers fail to start with
    # a secret-access error rather than a useful message.
    google_secret_manager_secret_iam_member.worker_reads_anthropic,
    # Puts the pool downstream of the sleep, so teardown is:
    # pool -> brief wait -> subnet (see time_sleep.vpc_release for the caveat).
    time_sleep.vpc_release,
  ]
}
