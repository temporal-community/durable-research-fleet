variable "region" {
  description = "AWS Region for the Runtime. Keep it next to the Temporal Cloud Namespace's Region."
  type        = string
  default     = "ca-central-1"
}

variable "prefix" {
  description = "Prefix for every resource name"
  type        = string
  default     = "research-fleet"
}

# --- Temporal Cloud: ONE Namespace ----------------------------------------------------
#
# These two have no defaults on purpose. This stack is wired to exactly one Namespace,
# and the validation below refuses anything that is not a Temporal Cloud Namespace id
# (`<name>.<account-id>`), so a self-hosted address or a typo cannot be applied.

variable "temporal_namespace" {
  description = "Temporal Cloud Namespace, `<name>.<account-id>`"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]*\\.[a-z0-9]+$", var.temporal_namespace))
    error_message = "temporal_namespace must be a Temporal Cloud Namespace id like my-ns.a1b2c."
  }
}

variable "temporal_address" {
  description = "Namespace gRPC endpoint, `<namespace>.tmprl.cloud:7233`"
  type        = string

  validation {
    condition     = endswith(var.temporal_address, ".tmprl.cloud:7233")
    error_message = "temporal_address must be a Temporal Cloud endpoint ending .tmprl.cloud:7233."
  }
}

variable "task_queue" {
  type    = string
  default = "research-queue"
}

variable "deployment_name" {
  description = "Worker Deployment name. Distinct from any other deployment in the Namespace."
  type        = string
  default     = "research-fleet-agentcore"
}

# --- Versioning ------------------------------------------------------------------------
#
# One Build ID == one Runtime VERSION == one endpoint. This is the AgentCore form of the
# Lambda rule "map each Build ID to an immutable function version": an endpoint is pinned
# to a Runtime version, and Temporal's Worker Deployment Version points at the endpoint,
# so a PINNED Workflow keeps landing on the code it started on.

variable "build_id" {
  description = "Build ID of the code in this apply. Changing it publishes a new Runtime version and endpoint."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$", var.build_id))
    error_message = "build_id must be 1-41 characters of letters, digits, '.', '_' or '-'."
  }
}

variable "retained_build_ids" {
  description = <<-E
    Build IDs that already have an endpoint and must KEEP it, because Workflows pinned to
    them are still running or parked in human review. List only builds that were already
    applied; a build listed here for the first time is created pointing at the CURRENT
    version. Remove an id only once `temporal worker deployment describe-version` shows
    that Version drained.
  E
  type        = set(string)
  default     = []
}

# --- Session behaviour ----------------------------------------------------------------

variable "idle_runtime_session_timeout" {
  description = "Seconds AgentCore keeps an idle session before reclaiming it"
  type        = number
  default     = 900
}

variable "max_lifetime" {
  description = "Hard session ceiling in seconds (AgentCore maximum is 28800)"
  type        = number
  default     = 28800

  validation {
    condition     = var.max_lifetime >= 60 && var.max_lifetime <= 28800
    error_message = "max_lifetime must be between 60 and 28800 seconds (8 hours)."
  }
}

variable "debounce_seconds" {
  description = "How long a Worker keeps polling after its last Activity before it drains. Sessions then linger for idle_runtime_session_timeout before AgentCore reclaims the microVM."
  type        = number
  default     = 60

  validation {
    condition     = var.debounce_seconds >= 5
    error_message = "debounce_seconds must be at least 5; 0 would drain the Worker before it served anything."
  }
}

variable "secret_recovery_window_days" {
  description = "Secrets Manager recovery window. A deleted secret's NAME stays reserved for this long, so `make ac-down` then `make ac-apply` fails within it; set 0 for a throwaway stack, 7-30 for a real one."
  type        = number
  default     = 7

  validation {
    condition     = var.secret_recovery_window_days == 0 || (var.secret_recovery_window_days >= 7 && var.secret_recovery_window_days <= 30)
    error_message = "secret_recovery_window_days must be 0 or between 7 and 30."
  }
}

variable "graceful_shutdown_seconds" {
  description = "How long a terminating session waits for in-flight Activities"
  type        = number
  default     = 120
}

variable "max_concurrent_activities" {
  description = <<-E
    Activity slots per session. 3 on AgentCore, unlike the 1 that Cloud Run needs.

    On Cloud Run one slot per instance is the demo (N sub-questions == N Workers) and is what
    creates the backlog the scaler reacts to. AgentCore does not map that way: Temporal
    chose 3-4 sessions for 5-6 sub-questions, so with 1 slot the extras QUEUED behind busy
    sessions (measured 2026-10-05: 61-69s waits, ~70s on a 4m36s run). Several slots let the
    sessions that do start take the whole fan-out at once. The cost is the old isolation:
    Activities in one session share a process. These are API-bound, not memory-bound.
    Whether Temporal then starts FEWER sessions is unmeasured; check peak distinct Workers.
  E
  type        = number
  default     = 3

  validation {
    condition     = var.max_concurrent_activities >= 1 && var.max_concurrent_activities <= 10
    error_message = "max_concurrent_activities must be between 1 and 10."
  }
}

variable "gemini_model" {
  type    = string
  default = "gemini-3.6-flash"
}

variable "anthropic_model" {
  type    = string
  default = "claude-opus-5"
}

variable "anthropic_research_model" {
  description = "Claude model for the research SUB-QUESTIONS only; planning and synthesis use anthropic_model. Empty means the main model."
  type        = string
  default     = "claude-sonnet-5-5"
}

variable "code_zip" {
  description = "Path to the Runtime code zip built by agentcore/build.sh"
  type        = string
  default     = "build/code.zip"
}

# The roles Temporal Cloud's Worker Controller assumes to invoke a Runtime. These are
# Temporal's published principals (the same list as the Lambda role in Temporal's own
# CloudFormation template for AgentCore); the ExternalId condition is what stops any
# OTHER Temporal tenant using the role.
variable "temporal_invoker_principals" {
  type = list(string)
  default = [
    "arn:aws:iam::902542641901:role/wci-lambda-invoke",
    "arn:aws:iam::160190466495:role/wci-lambda-invoke",
    "arn:aws:iam::819232936619:role/wci-lambda-invoke",
    "arn:aws:iam::829909441867:role/wci-lambda-invoke",
    "arn:aws:iam::354116250941:role/wci-lambda-invoke",
  ]
}
