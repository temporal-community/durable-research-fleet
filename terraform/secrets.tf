# The research app's Claude API key.
#
# In Secret Manager rather than a plaintext container env var for one concrete
# reason: `terraform.tfstate` on this repo has already been a real exposure (it is
# in .gitignore because two state files were committed once), and a `sensitive`
# variable still lands in state in the clear. A secret_key_ref does not.
#
# Only the WORKER needs it. The web tier runs the same image but never calls
# Claude — web.py starts and queries Workflows by name and deliberately imports
# neither `llm` nor `anthropic` (enforced by
# tests/test_serverless_contract.py::test_the_web_tier_does_not_import_the_research_app),
# so it gets no access to this secret at all.

resource "google_secret_manager_secret" "anthropic" {
  secret_id = "${local.prefix}-anthropic-api-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

# Written only when a key is supplied. Leaving var.anthropic_api_key null means
# the secret exists but empty, the hello app still works end to end, and
# `make verify SCALE=1` still passes — the research app is the only thing that
# needs it. Populate out of band with:
#
#   printf %s "$ANTHROPIC_API_KEY" | gcloud secrets versions add \
#     research-fleet-anthropic-api-key --data-file=- --project <project>
#
# which keeps the key out of shell history and out of Terraform state entirely.
resource "google_secret_manager_secret_version" "anthropic" {
  count = var.anthropic_api_key == null ? 0 : 1

  secret      = google_secret_manager_secret.anthropic.id
  secret_data = var.anthropic_api_key
}

# The pool's containers read the secret at start. Scoped to this one secret rather
# than granting a project-wide role.
resource "google_secret_manager_secret_iam_member" "worker_reads_anthropic" {
  secret_id = google_secret_manager_secret.anthropic.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker_rt.email}"
}
