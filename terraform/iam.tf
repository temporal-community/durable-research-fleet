# Three distinct identities. Keeping them separate is the point — the thing that
# can scale the pool cannot run your code, and vice versa.
#
#   invoker      -> reads + resizes the Worker Pool. Never runs anything.
#   temporal_vm  -> the Temporal Service itself. Impersonates the invoker.
#   worker_rt    -> what the pool's containers actually run as.

resource "google_service_account" "invoker" {
  account_id   = "${local.prefix}-invoker"
  display_name = "Temporal Serverless Worker Pool Invoker"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "temporal_vm" {
  account_id   = "${local.prefix}-temporal-vm"
  display_name = "Temporal self-hosted server (GCE)"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "worker_rt" {
  account_id   = "${local.prefix}-worker-rt"
  display_name = "Research Fleet — Worker runtime"
  depends_on   = [google_project_service.apis]
}

# The docs ask for "run.developer or equivalent (must include run.workerPools.get and
# run.workerPools.update)". We grant the equivalent rather than the predefined role:
# project-level roles/run.developer also lets the invoker create, update and DELETE
# every other Cloud Run resource in this project — including the public web Service.
# The invoker is the identity Temporal impersonates, so keep it to the two verbs the
# WCI actually calls.
#
# If scaling ever stops working after touching this, the missing permission shows up
# in the Temporal server log, not in GCP:
#   sudo journalctl -u temporal | grep UpdateWorkerSetSize
resource "google_project_iam_custom_role" "worker_pool_scaler" {
  role_id     = "workerPoolScaler"
  title       = "Serverless Workers — Worker Pool scaler"
  description = "Minimum for the Temporal invoker SA to read and resize a Cloud Run Worker Pool."
  permissions = [
    "run.workerPools.get",
    "run.workerPools.update",
  ]
}

resource "google_project_iam_member" "invoker_pool_scaler" {
  project = var.project_id
  role    = google_project_iam_custom_role.worker_pool_scaler.id
  member  = "serviceAccount:${google_service_account.invoker.email}"
}

# The pool-scaling permissions above are not sufficient on their own.
#
# A Worker Pool's template names a runtime service account, so resizing the pool
# is an update that "sets" that identity, and Cloud Run therefore requires
# iam.serviceAccounts.actAs on the RUNTIME SA as well. Without this binding the
# Worker Controller's UpdateWorkerSetSize Activity fails on every attempt with:
#
#   failed to update worker pool ".../research-fleet-worker-pool": rpc error:
#   code = PermissionDenied desc = Permission 'iam.serviceaccounts.actAs'
#   denied on service account research-fleet-worker-rt@...
#
# and the pool silently never scales — the error only appears in the Temporal
# server's own debug log, not anywhere in GCP.
#
# This is documented: docs.temporal.io/production-deployment/worker-deployments/
# serverless-workers/cloud-run#runner-service-account. It is still the easiest
# binding to omit, because nothing tells you it is missing.
resource "google_service_account_iam_member" "invoker_acts_as_worker_rt" {
  service_account_id = google_service_account.worker_rt.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.invoker.email}"
}

# The impersonation hop: the Temporal Service becomes the invoker. On GCE the
# server picks this identity up from the metadata server as ADC automatically —
# no key files anywhere.
resource "google_service_account_iam_member" "vm_impersonates_invoker" {
  service_account_id = google_service_account.invoker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.temporal_vm.email}"
}

resource "google_service_account_iam_member" "extra_sa_impersonators" {
  for_each           = toset(var.impersonator_service_account_emails)
  service_account_id = google_service_account.invoker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${each.value}"
}

resource "google_service_account_iam_member" "user_impersonators" {
  for_each           = toset(var.impersonator_user_emails)
  service_account_id = google_service_account.invoker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "user:${each.value}"
}

# The VM needs to write logs and to pull the CLI binary out of the bootstrap
# bucket (bucket-scoped, not project-wide).
resource "google_project_iam_member" "vm_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.temporal_vm.email}"
}

resource "google_storage_bucket_iam_member" "vm_reads_bootstrap" {
  bucket = google_storage_bucket.bootstrap.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.temporal_vm.email}"
}
