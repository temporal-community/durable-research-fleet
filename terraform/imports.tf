# Optional adoption of resources that already exist with this stack's names —
# for migrating a hand-built environment without destroying anything (notably the
# Artifact Registry repo, whose deletion would take the pushed image with it).
#
# OFF by default. A clean project must not try to import: `terraform apply` fails
# at plan time on "Cannot import non-existent remote object". Enable only when
# adopting:
#
#     terraform apply -var="adopt_existing=true"
#
# Then, once state holds them, drop the flag again (or delete this file).
#
# Project and region come from the variables, so this is not pinned to any one
# environment.

variable "adopt_existing" {
  description = "Import pre-existing repo/VPC/subnet that already match this stack's names, instead of creating them. Leave false on a clean project."
  type        = bool
  default     = false
}

import {
  for_each = var.adopt_existing ? toset(["adopt"]) : toset([])
  to       = google_artifact_registry_repository.repo
  id       = "projects/${var.project_id}/locations/${var.region}/repositories/${var.name_prefix}-repo"
}

import {
  for_each = var.adopt_existing ? toset(["adopt"]) : toset([])
  to       = google_compute_network.vpc
  id       = "projects/${var.project_id}/global/networks/${var.name_prefix}-vpc"
}

import {
  for_each = var.adopt_existing ? toset(["adopt"]) : toset([])
  to       = google_compute_subnetwork.subnet
  id       = "projects/${var.project_id}/regions/${var.region}/subnetworks/${var.name_prefix}-subnet"
}
