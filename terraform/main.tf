provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# Auto-detect the operator's public IP so the SSH rule is narrow by default
# instead of 0.0.0.0/0. Override with var.ssh_source_ranges.
data "http" "my_ip" {
  count = var.ssh_source_ranges == null ? 1 : 0
  url   = "https://api.ipify.org"
}

locals {
  prefix = var.name_prefix

  ssh_ranges = var.ssh_source_ranges != null ? var.ssh_source_ranges : [
    "${chomp(data.http.my_ip[0].response_body)}/32"
  ]

  worker_image = coalesce(
    var.worker_image,
    "${var.region}-docker.pkg.dev/${var.project_id}/${local.prefix}-repo/research-worker:${var.build_id}"
  )

  temporal_address = "${var.temporal_internal_ip}:7233"

  # THE authoritative Worker Controller config for the VM.
  #
  # `temporal server start-dev` has no --dynamic-config-file flag (only repeated
  # --dynamic-config-value), so a YAML file cannot be handed to it. Rather than
  # write a YAML file the server never reads, the values live here and are
  # rendered into the launch flags. selfhosted/dynamicconfig/wci.yaml is for the
  # compose stack only, which runs the full server and does read a file.
  wci_dynamic_config = {
    "workercontroller.enabled" = "true"
    # Which compute providers the WCI may drive.
    "workercontroller.compute_providers.enabled" = jsonencode(["gcp-cloud-run"])
    # BOTH are required for Cloud Run. The published self-hosted docs list only
    # no-sync (that is the Lambda setup); without rate-based, create-version
    # fails with "Could not instantiate scaling algorithm with type 'rate-based'".
    "workercontroller.scaling_algorithms.enabled" = jsonencode(["no-sync", "rate-based"])
    # Hard ceiling on how far the WCI may scale the pool — the cost guard.
    #
    # CAVEAT: this key name was recovered from the server binary's string table
    # (it is not in the published dynamic config reference). The server accepts
    # it and starts cleanly, but unknown dynamic config keys are accepted
    # silently, so "it starts" is not proof it is honoured. Treat the cap as
    # best-effort until you have watched a burst actually stop at this number,
    # and don't rely on it alone to bound spend.
    "workercontroller.maxInstances" = tostring(var.max_instances)
  }

  # Rendered into a launcher script rather than straight into systemd's
  # ExecStart, so systemd never has to parse quotes around the JSON values.
  wci_flags = join(" \\\n  ", [
    for k, v in local.wci_dynamic_config : "--dynamic-config-value '${k}=${v}'"
  ])
}

# APIs. disable_on_destroy stays false: turning APIs off on destroy can break
# unrelated things in a shared project, and leaving them on costs nothing.
resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com",
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "storage.googleapis.com",
    "logging.googleapis.com",
    # Required for the server to mint tokens for the invoker SA. Without it,
    # create-version fails with iam.serviceAccounts.getAccessToken denied.
    "iamcredentials.googleapis.com",
    # Needed to create the service accounts in iam.tf. On a project that has
    # never used IAM, apply otherwise dies half-built with SERVICE_DISABLED.
    "iam.googleapis.com",
    # Needed for project-level IAM bindings (google_project_iam_member).
    "cloudresourcemanager.googleapis.com",
    # The research app's Gemini key lives in Secret Manager rather than in a
    # plaintext env var, because Terraform state is already a known exposure on
    # this repo (*.tfstate* is gitignored precisely because it leaked once).
    "secretmanager.googleapis.com",
  ])

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}
