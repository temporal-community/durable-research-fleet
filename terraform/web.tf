# The single-page research console — audience and projector, one URL.
#
# THIS IS A CLOUD RUN *SERVICE*, NOT A WORKER POOL. The two are different products
# with different scaling models and it is the single easiest thing to conflate in
# this whole stack:
#
#   google_cloud_run_v2_worker_pool  long-lived instances that POLL Temporal.
#                                    Resized 0->N by the Worker Controller.
#                                    These are the Serverless Workers.
#   google_cloud_run_v2_service      request-serving. Scales on HTTP traffic like
#                                    any web app. This is an ordinary Temporal
#                                    CLIENT and is NOT a Serverless Worker.
#
# It runs the SAME IMAGE as the pool, with the container command overridden to
# start uvicorn instead of the Worker. One build, one push, two deployments — and
# no way for the two tiers to drift onto different versions of the shared modules.

resource "google_service_account" "web_rt" {
  account_id   = "${local.prefix}-web-rt"
  display_name = "Research Fleet — web tier runtime"
  depends_on   = [google_project_service.apis]
}

resource "google_artifact_registry_repository_iam_member" "web_rt_reader" {
  location   = google_artifact_registry_repository.repo.location
  repository = google_artifact_registry_repository.repo.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.web_rt.email}"
}

resource "google_cloud_run_v2_service" "web" {
  name     = "${local.prefix}-web"
  location = var.region

  # Same reasoning as the Worker Pool: teardown has to be reliable on a demo stack,
  # and the provider default of true makes `terraform destroy` fail partway.
  deletion_protection = false

  # NOT internet-reachable. Changed 2026-07-31 after security flagged the public
  # endpoint: Cloud Run's newer URL form is `<service>-<project-number>.<region>
  # .run.app`, and both the service name and region are in this public repo, so the
  # hostname was derivable rather than obscure. The earlier "nobody can find it"
  # reasoning (wildcard cert, so no Certificate Transparency entry) did not hold.
  #
  # The demo no longer depends on this Service. The presenter runs the console
  # locally (`make web-local`) against this project's Temporal over the SSH tunnel,
  # so the fan-out and scale-from-zero are still real Cloud Run behaviour with no
  # public surface. Set this back to INGRESS_TRAFFIC_ALL only with a documented
  # org-policy exception, not by reaching for invoker_iam_disabled again.
  ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  # Authentication required. This was `true` until 2026-07-31.
  #
  # History, because the reasoning matters: this org enforces domain-restricted
  # sharing, so `allUsers` + roles/run.invoker is refused outright. Disabling the
  # invoker IAM check is Google's documented answer to that, and it worked — but it
  # produces exactly the same exposure as the binding the org policy exists to
  # prevent, and the Cloud Run console reports the Service as "Authentication:
  # Public Access" no matter what `ingress` is set to. That is what security's scan
  # sees, so internal-only ingress alone does not close the finding.
  #
  # Access is now an ordinary IAM grant to named identities (below), which are
  # in-domain and therefore permitted by the policy. Nothing is granted by default.
  invoker_iam_disabled = false

  template {
    service_account = google_service_account.web_rt.email

    scaling {
      min_instance_count = 0
      # A conference room is a burst of phones, not sustained traffic.
      max_instance_count = 4
    }

    containers {
      image = local.worker_image

      # THE override that makes one image serve two roles. Without it this Service
      # would start a second Worker — which would poll the Task Queue, absorb the
      # burst, and quietly destroy the scaling demo by making the pool unnecessary.
      command = ["uvicorn"]
      args = [
        "web:app",
        "--host", "0.0.0.0",
        # Cloud Run injects PORT; 8080 is its default and what we bind.
        "--port", "8080",
        # Uvicorn's proxy-header middleware is ON by default and REWRITES
        # `request.client` from the FIRST X-Forwarded-For entry — which is the one
        # value a caller can forge. `web._client_ip` does its own trusted-hop
        # parsing, so leaving this on would silently poison the fallback it relies
        # on and make the per-IP ask limit bypassable.
        "--no-proxy-headers",
      ]

      ports {
        container_port = 8080
      }

      env {
        name  = "TEMPORAL_ADDRESS"
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
        # Plaintext gRPC to the self-hosted frontend, same as the pool.
        name  = "TEMPORAL_TLS"
        value = "false"
      }
      env {
        # Optional shared passcode announced from the stage. Empty means open.
        name  = "DEMO_PASSCODE"
        value = var.demo_passcode
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 3
        period_seconds        = 3
        failure_threshold     = 10
      }
    }

    # Direct VPC egress, exactly as the pool does it — the web tier has to reach
    # the Temporal frontend on its private address. PRIVATE_RANGES_ONLY keeps
    # RFC1918 traffic in the VPC and sends everything else out normally.
    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"

      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.subnet.id
      }
    }
  }

  lifecycle {
    ignore_changes = [
      # Same drift sources as the Worker Pool: stamped by whoever wrote last.
      client,
      client_version,
    ]
  }

  depends_on = [
    google_project_service.apis,
    google_artifact_registry_repository_iam_member.web_rt_reader,
    time_sleep.vpc_release,
  ]
}

# PUBLIC AND UNAUTHENTICATED, ON PURPOSE — the audience opens this on their phones
# and cannot be asked to authenticate to GCP.
#
# It is also the sharpest edge in this stack: an unauthenticated POST starts a
# Workflow that spends money on Claude tokens. Four things bound the damage, and
# all four should stay in place:
#
#   1. var.demo_passcode         a shared code announced from the stage
#   2. ASKS_PER_MINUTE_PER_IP    per-IP rate limit in web.py
#   3. MAX_QUESTION_CHARS        question length cap in web.py
#   4. var.max_instances         the hard ceiling on Serverless Workers, which is
#                                what actually bounds total spend
#
# There is deliberately NO endpoint here that can scale the pool. Chaos-testing on
# stage is a `gcloud run worker-pools update` from a terminal, so this public
# Service never needs Cloud Run write permission.
# NO `allUsers` IAM BINDING HERE — ON PURPOSE.
#
# This used to be:
#
#   resource "google_cloud_run_v2_service_iam_member" "web_public" {
#     role   = "roles/run.invoker"
#     member = "allUsers"
#   }
#
# It cannot be created in this organization. Domain-restricted sharing rejects the
# special principals `allUsers` and `allAuthenticatedUsers`, so the apply failed
# with HTTP 400 while the other 24 resources came up clean.
#
# It is no longer wanted either. Access is named identities only:
resource "google_cloud_run_v2_service_iam_member" "web_invokers" {
  for_each = toset(var.web_invoker_users)

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.web.name
  role     = "roles/run.invoker"
  member   = "user:${each.value}"
}
#
# Don't reintroduce this binding. The alternatives that do NOT work here:
#   - an external HTTPS Load Balancer in front of Cloud Run still requires
#     `allUsers` invoker on the Service, so it hits the same wall;
#   - a project owner cannot override an inherited org policy — the DRS exception
#     route needs roles/orgpolicy.policyAdmin at the org or folder, plus custom
#     org policies with resource tags, which is a people-process dependency this
#     stack should not have.
