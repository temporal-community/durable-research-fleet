resource "google_artifact_registry_repository" "repo" {
  repository_id = "${local.prefix}-repo"
  location      = var.region
  format        = "DOCKER"
  description   = "Research Fleet worker images"
  depends_on    = [google_project_service.apis]
}

# The pool's runtime SA needs to pull the image.
resource "google_artifact_registry_repository_iam_member" "worker_rt_reader" {
  repository = google_artifact_registry_repository.repo.name
  location   = google_artifact_registry_repository.repo.location
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.worker_rt.email}"
}

# Ships the main-branch `temporal` binary to the VM. Needed because the Cloud Run
# compute provider is not in any released server, so there is no public image or
# release asset we can just curl.
resource "google_storage_bucket" "bootstrap" {
  name                        = "${var.project_id}-${local.prefix}-bootstrap"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  depends_on                  = [google_project_service.apis]
}

resource "google_storage_bucket_object" "temporal_cli" {
  name   = "temporal-linux-amd64"
  bucket = google_storage_bucket.bootstrap.name
  source = var.temporal_cli_binary

  lifecycle {
    precondition {
      condition     = fileexists(var.temporal_cli_binary)
      error_message = <<-EOT
        temporal_cli_binary not found at "${var.temporal_cli_binary}".

        The Cloud Run compute provider only works on the server bundled in the
        MAIN branch of temporalio/cli, so you must build it yourself:

          git clone --depth 1 https://github.com/temporalio/cli.git /tmp/tcli
          cd /tmp/tcli && GOOS=linux GOARCH=amd64 go build -o temporal-linux ./cmd/temporal
          cp temporal-linux <repo>/bin/temporal-linux

        Or run `make cli` from the repo root.
      EOT
    }
  }
}
