variable "project_id" {
  description = "GCP project that hosts everything in this stack"
  type        = string
  default     = "serverless-workers-demo"
}

variable "region" {
  type    = string
  default = "us-east4"
}

variable "zone" {
  type    = string
  default = "us-east4-a"
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

# On a rotating-egress VPN (Cloudflare WARP, corporate split-tunnel) the
# auto-detected /32 goes stale without warning: the rule still looks correct in
# the console, and SSH fails with a TIMEOUT rather than a refusal, which reads as
# "the VM is down" instead of "your IP moved". Observed rotating twice in an hour
# and across a /16, so widening to the VPN's range is sometimes the only way to
# hold a tunnel open -- but that opens port 22 to every tenant sharing that
# range, so widen deliberately, set it back to null afterwards, and never leave a
# widened range in a committed tfvars.
#
# Check what the rule actually allows before debugging anything else:
#   curl -s https://api.ipify.org
#   gcloud compute firewall-rules describe <prefix>-allow-ssh --format='value(sourceRanges.list())'
variable "ssh_source_ranges" {
  description = "CIDRs allowed to SSH the Temporal VM. Leave null to auto-detect this machine's public IP; set [] to block SSH entirely. Auto-detect goes stale behind a rotating-egress VPN -- see the note above."
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

# Deleting a custom role does not free its id. GCP keeps the role recoverable for 7
# days, purges it after 30, and RESERVES THE ID FOR 37 — so between day 7 and day 37 a
# torn-down stack can neither undelete the old role nor create a new one with the same
# id, and `make up` dies mid-apply on:
#
#   Error 400: You can't create a role_id (workerPoolScaler) which has been marked
#   for deletion, failedPrecondition
#
# while `gcloud iam roles undelete` answers NOT_FOUND. Hit on 2026-09-06 redeploying a
# stack torn down more than a week earlier. Bump this suffix to get a fresh id; the old
# tombstone ages out on its own. Nothing reads the role by name — verify_stack.sh
# deliberately checks the two verbs instead (scripts/verify_stack.sh §7).
variable "scaler_role_suffix" {
  description = "Suffix for the custom scaler role id; bump if the previous id is tombstoned"
  type        = string
  default     = ""
}
