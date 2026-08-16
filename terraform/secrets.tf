# The research app's Gemini Developer API key.
#
# The key is referenced from Secret Manager rather than placed in a plain
# environment variable in Terraform: marking a Terraform variable sensitive only
# hides terminal output; its value still lands in state. The web Service never calls
# Gemini and receives neither this secret nor permission to read it.

resource "google_secret_manager_secret" "gemini" {
  secret_id = "${local.prefix}-gemini-api-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

# Optional convenience path. Prefer populating the secret out of band:
#
#   printf %s "$GEMINI_API_KEY" | gcloud secrets versions add \
#     research-fleet-gemini-api-key --data-file=- --project <project>
resource "google_secret_manager_secret_version" "gemini" {
  count = var.gemini_api_key == null ? 0 : 1

  secret      = google_secret_manager_secret.gemini.id
  secret_data = var.gemini_api_key
}

resource "google_secret_manager_secret_iam_member" "worker_reads_gemini" {
  secret_id = google_secret_manager_secret.gemini.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker_rt.email}"
}
