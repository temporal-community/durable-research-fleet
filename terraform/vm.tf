resource "google_compute_instance" "temporal" {
  name         = "${local.prefix}-temporal"
  machine_type = var.machine_type
  zone         = var.zone
  tags         = ["${local.prefix}-temporal"]

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = 20
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.subnet.id
    network_ip = google_compute_address.temporal_internal.address

    # Ephemeral external IP purely for outbound package/binary fetches. No
    # ingress is opened to it beyond the scoped SSH rule. Swap for Cloud NAT if
    # you want zero public addresses.
    access_config {}
  }

  service_account {
    email  = google_service_account.temporal_vm.email
    scopes = ["cloud-platform"]
  }

  metadata_startup_script = templatefile("${path.module}/templates/startup.sh.tftpl", {
    bootstrap_bucket = google_storage_bucket.bootstrap.name
    cli_object       = google_storage_bucket_object.temporal_cli.name
    # Rendered --dynamic-config-value flags. start-dev has no
    # --dynamic-config-file, so a YAML file could never have been read here.
    wci_flags          = local.wci_flags
    temporal_namespace = var.temporal_namespace
    deployment_name    = var.deployment_name
    build_id           = var.build_id
    gcp_project        = var.project_id
    gcp_region         = var.region
    worker_pool        = google_cloud_run_v2_worker_pool.fleet.name
    invoker_sa         = google_service_account.invoker.email
  })

  # The bootstrap script calls create-version, which immediately impersonates the
  # invoker and touches the pool — so every one of these must exist first or the
  # VM's first boot fails in a way that needs a manual re-run.
  depends_on = [
    google_service_account_iam_member.vm_impersonates_invoker,
    google_project_iam_member.invoker_run_developer,
    google_storage_bucket_iam_member.vm_reads_bootstrap,
    google_storage_bucket_object.temporal_cli,
    google_cloud_run_v2_worker_pool.fleet,
    google_compute_firewall.temporal_internal,
  ]

  allow_stopping_for_update = true
}
