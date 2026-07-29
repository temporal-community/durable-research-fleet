# A dedicated VPC: this project has no default network (org policy), and a
# purpose-built one lets the firewall be tight.
resource "google_compute_network" "vpc" {
  name                    = "${local.prefix}-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

# Shared by the Temporal VM and by Cloud Run Direct VPC egress — that shared
# membership is what lets the pool reach Temporal on a private address with no
# tunnel and no public exposure.
resource "google_compute_subnetwork" "subnet" {
  name          = "${local.prefix}-subnet"
  network       = google_compute_network.vpc.id
  region        = var.region
  ip_cidr_range = var.subnet_cidr
}

resource "google_compute_address" "temporal_internal" {
  name         = "${local.prefix}-temporal-ip"
  subnetwork   = google_compute_subnetwork.subnet.id
  address_type = "INTERNAL"
  address      = var.temporal_internal_ip
  region       = var.region
}

# The Temporal frontend is reachable ONLY from inside the subnet. It is never
# exposed to the internet, which is the main reason this beats a public tunnel.
resource "google_compute_firewall" "temporal_internal" {
  name          = "${local.prefix}-allow-temporal-internal"
  network       = google_compute_network.vpc.name
  description   = "Cloud Run Worker Pool -> Temporal frontend, private only"
  source_ranges = [var.subnet_cidr]
  target_tags   = ["${local.prefix}-temporal"]

  allow {
    protocol = "tcp"
    ports    = ["7233"]
  }
}

resource "google_compute_firewall" "ssh" {
  count = length(local.ssh_ranges) > 0 ? 1 : 0

  name          = "${local.prefix}-allow-ssh"
  network       = google_compute_network.vpc.name
  description   = "SSH for setup/debug, scoped to the operator"
  source_ranges = local.ssh_ranges
  target_tags   = ["${local.prefix}-temporal"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# Cloud Run Direct VPC egress leaves an address reservation behind on teardown.
#
# The pool's egress makes Cloud Run reserve an internal address in this subnet
# (`serverless-ipv4-<id>`). After the pool is deleted that address stays
# `RESERVED`, held by a Cloud-Run-managed reservation:
#
#   //serverless.googleapis.com/projects/<n>/locations/<region>/addressReservations/serverless-ipv4-<id>
#
# It CANNOT be deleted directly (`gcloud compute addresses delete` refuses —
# "already being used by ... addressReservations/..."), and GCP releases it
# asynchronously on its own schedule. Observed release time on this project:
# roughly 40 minutes — far longer than any sensible wait. So `terraform destroy` may fail to remove the subnet and VPC with:
#
#   Error when reading or editing Subnetwork: ... resourceInUseByAnotherResource
#
# Everything else is destroyed. `make down` is safe to re-run and completes the
# subnet + VPC once GCP has released the reservation. A leftover subnet and VPC
# cost nothing, so a delayed final cleanup is harmless.
#
# This short sleep covers the case where the release IS quick.
resource "time_sleep" "vpc_release" {
  depends_on       = [google_compute_subnetwork.subnet]
  destroy_duration = "60s"
}
