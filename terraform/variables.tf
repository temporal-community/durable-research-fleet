variable "project_id" {
  description = "GCP project that hosts everything in this stack"
  type        = string
  default     = "serverless-workers-demo"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "name_prefix" {
  description = "Prefix for every resource name, so the whole stack is easy to spot and delete"
  type        = string
  default     = "research-fleet"
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

variable "subnet_cidr" {
  description = "Range for the subnet shared by the Temporal VM and Cloud Run Direct VPC egress"
  type        = string
  default     = "10.10.0.0/24"
}

variable "temporal_internal_ip" {
  description = "Static internal IP for the Temporal VM. Static so the Worker Pool's TEMPORAL_ADDRESS never churns. Must sit inside subnet_cidr."
  type        = string
  default     = "10.10.0.10"
}

variable "ssh_source_ranges" {
  description = "CIDRs allowed to SSH the Temporal VM. Leave null to auto-detect this machine's public IP; set [] to block SSH entirely."
  type        = list(string)
  default     = null
}

# ---------------------------------------------------------------------------
# Temporal VM
# ---------------------------------------------------------------------------

variable "machine_type" {
  description = "e2-small (2 GB) is enough for the single-binary dev server; the startup script also adds 2 GB of swap."
  type        = string
  default     = "e2-small"
}

variable "temporal_cli_binary" {
  description = <<-EOT
    Path to a linux/amd64 `temporal` binary built from the MAIN branch of
    temporalio/cli. Required: the Cloud Run compute provider needs the server
    bundled in main (1.32.0-158.0+). Released images/binaries — including
    temporalio/server:1.31.2 — accept the config keys but then fail with
    "Could not instantiate scaling algorithm with type 'rate-based'".

    Build with:
      git clone --depth 1 https://github.com/temporalio/cli.git
      cd cli && GOOS=linux GOARCH=amd64 go build -o temporal-linux ./cmd/temporal
  EOT
  type        = string
  default     = "../bin/temporal-linux"
}

# ---------------------------------------------------------------------------
# Worker Pool / Temporal deployment
# ---------------------------------------------------------------------------

variable "worker_image" {
  description = "Fully-qualified image for the worker pool. Push it before applying (see the Makefile); Terraform does not build images."
  type        = string
  default     = null
}

variable "temporal_namespace" {
  type    = string
  default = "default"
}

variable "task_queue" {
  type    = string
  default = "research-queue"
}

variable "deployment_name" {
  description = "Worker Deployment name; must match what worker_cloudrun.py registers"
  type        = string
  default     = "research-fleet"
}

variable "build_id" {
  description = "Worker Deployment Version build id; must match the pool's BUILD_ID env var"
  type        = string
  default     = "v1"
}

# NOTE ON no_sync_quiet_ms (KNOWLEDGE_BASE.md §7.3)
#
# There is deliberately no variable for it, because nothing in this stack can
# set it today. It is a field on the Worker Deployment Version's scaling-algorithm
# compute config (it appears in the server binary only as a protobuf field name,
# alongside dispatch_rate / desired_count / catch_up_rate), and:
#   - `temporal worker deployment create-version` on main exposes no flag for it
#   - it is not a dynamic config key (no workercontroller.* entry exists)
# So it is settable only through the Temporal Cloud UI's "Scaling and Lifecycle"
# panel or the raw API. A variable here would silently do nothing.
#
# The mitigation that DOES work on this path is Activity Heartbeats plus a
# heartbeat_timeout — see workflows.py and activities.py. Revisit when the CLI
# grows a flag.

variable "max_instances" {
  description = "Upper bound the WCI may scale the pool to. Applied as the workercontroller.maxInstances dynamic config value on the Temporal VM. This is the ceiling that actually bounds spend on a public demo: one question fans out to ~6 sub-questions and one Activity slot per instance, so 30 comfortably absorbs a room of ~20 phones."
  type        = number
  default     = 30
}

variable "max_concurrent_activities" {
  description = "Activity slots per pool instance. 1 gives KB §4 slot isolation and guarantees a burst produces real Task Queue backlog for the WCI to react to. Raise only if your Activity is cheap and you have headroom."
  type        = number
  default     = 1
}

variable "graceful_shutdown_seconds" {
  description = "How long the Worker may drain in-flight Activities after SIGTERM. Keep below Cloud Run's termination grace period or the process is SIGKILLed mid-drain."
  type        = number
  default     = 20
}

variable "worker_cpu" {
  type    = string
  default = "1"
}

variable "worker_memory" {
  description = "1Gi rather than 512Mi: the image carries the Google Gen AI SDK and research Activities can hold large grounded responses in memory. The hello app fits in far less."
  type        = string
  default     = "1Gi"
}

# ---------------------------------------------------------------------------
# Research app
# ---------------------------------------------------------------------------

variable "gemini_api_key" {
  description = <<-EOT
    Gemini Developer API key for the research app. Leave null (the default) and populate the
    secret out of band instead, which keeps the key out of Terraform state:

      printf %s "$GEMINI_API_KEY" | gcloud secrets versions add \
        research-fleet-gemini-api-key --data-file=- --project <project>

    Setting it here works but writes the key into terraform.tfstate in the clear.
    Only the Worker Pool reads it; the web tier never calls Gemini.
  EOT
  type        = string
  default     = null
  sensitive   = true
}

variable "anthropic_api_key" {
  description = <<-EOT
    Anthropic API key for the optional Claude UI selection. Leave null and populate
    Secret Manager out of band to keep it out of Terraform state:

      printf %s "$ANTHROPIC_API_KEY" | gcloud secrets versions add \
        research-fleet-anthropic-api-key --data-file=- --project <project>

    The Worker Pool reads it; the web tier receives neither provider key.
  EOT
  type        = string
  default     = null
  sensitive   = true
}

variable "demo_passcode" {
  description = "Optional shared passcode for the public phone page, announced from the stage. Empty means anyone who finds the URL can spend tokens — see the bounding controls in web.tf."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Temporal Cloud alternative (unused on the self-hosted path)
# ---------------------------------------------------------------------------

variable "impersonator_service_account_emails" {
  description = "Extra service accounts allowed to impersonate the invoker. On Temporal Cloud this is the identity the Cloud UI shows you. The Temporal VM's own SA is granted automatically."
  type        = list(string)
  default     = []
}

variable "impersonator_user_emails" {
  description = "User accounts allowed to impersonate the invoker — handy for running create-version from your laptop against the VM."
  type        = list(string)
  default     = []
}

variable "web_invoker_users" {
  description = "In-domain user accounts granted roles/run.invoker on the web Service. Empty — the default — means nobody can call it, which is correct now that the presenter runs the console locally against this project's Temporal. Never put allUsers or allAuthenticatedUsers here: domain-restricted sharing rejects them, and unauthenticated invocation is exactly what security flagged on 2026-07-31."
  type        = list(string)
  default     = []
}

variable "web_public" {
  description = "Make the web Service reachable from the internet with NO authentication: ingress ALL and invoker_iam_disabled = true. This is the configuration an external scan flagged on 2026-07-31, and the Cloud Run console will report the Service as 'Authentication: Public Access' regardless of anything else. It is here because a live conference demo where the room asks questions from their own phones has no other route on this org — domain-restricted sharing refuses allUsers, and browsers cannot satisfy a Cloud Run IAM check. demo_passcode is what bounds spend while it is open, so never set this true with an empty passcode. Set it ONLY in terraform/terraform.tfvars (gitignored), turn it off the same day, and rotate the passcode afterwards."
  type        = bool
  default     = false

  validation {
    condition     = var.web_public == false || length(var.demo_passcode) > 0
    error_message = "web_public = true requires a non-empty demo_passcode — an open page with no passcode lets anyone spend tokens against whichever provider key they select. Set it in terraform/terraform.tfvars, not with -var, or a plain `make apply` resets it to \"\"."
  }
}

variable "max_subquestions" {
  description = "Fan-out width: how many sub-questions one question becomes. With one Activity slot per instance this IS the Serverless Worker count the room watches. The planner always returns the top of its range, so this value — not the wording of the question — decides the width. 6 is the full demo; 3 gives a smaller, faster fan-out."
  type        = number
  default     = 6
}

variable "research_effort" {
  description = "Gemini thinking level for research calls: minimal, low, medium, or high. This is the demo's latency knob."
  type        = string
  default     = "medium"

  validation {
    condition     = contains(["minimal", "low", "medium", "high"], var.research_effort)
    error_message = "research_effort must be one of: minimal, low, medium, high."
  }
}

variable "gemini_model" {
  description = "Gemini model ID used by the research app. The default is the GA Gemini 3.6 Flash model."
  type        = string
  default     = "gemini-3.6-flash"
}

variable "anthropic_model" {
  description = "Claude model ID used when a Workflow selects Anthropic."
  type        = string
  default     = "claude-opus-5"
}
