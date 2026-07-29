terraform {
  required_version = ">= 1.5.7"

  required_providers {
    # 7.x is required for google_cloud_run_v2_worker_pool — Worker Pools do not
    # exist in the 4.x provider. This is also why this stack does NOT use
    # temporalio/terraform-modules//modules/serverless-workers/gcp/cloud-run:
    # that module pins google "~> 4.0", which is mutually exclusive with the
    # provider version we need, so `terraform init` cannot solve it. Its three
    # resources are inlined in iam.tf instead.
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
    # Only used to auto-detect the operator's public IP for the SSH firewall
    # rule, so the stack needs no manual input to be secure by default.
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
    # Used only for destroy ordering — see network.tf's vpc_release.
    time = {
      source  = "hashicorp/time"
      version = "~> 0.11"
    }
  }
}
