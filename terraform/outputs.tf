output "invoker_email" {
  description = "Identity Temporal impersonates to resize the pool"
  value       = google_service_account.invoker.email
}

output "temporal_vm_sa" {
  value = google_service_account.temporal_vm.email
}

output "temporal_address" {
  description = "Private address the Worker Pool dials"
  value       = local.temporal_address
}

output "worker_pool" {
  value = google_cloud_run_v2_worker_pool.fleet.name
}

output "worker_image" {
  description = "Image both the pool and the web Service run — push it with `make image`"
  value       = local.worker_image
}

# One URL for the room AND the projector. The separate /dashboard page was retired
# on 2026-07-29: the console carries the fleet counters in its footer and scales
# them with the viewport, so the same page serves a phone in a seat and a screen at
# the front of the room. There is deliberately no `dashboard_url` output any more.
#
# NOT hand-out-able since 2026-07-31. The Service is internal-ingress with invoker IAM
# on (CLAUDE.md gate #12), so this URL 403s for anyone not named in
# `var.web_invoker_users`. Kept because it is still how you identify the revision and
# tail its logs. To actually serve the console, run `make web-local` against this
# project's Temporal over the SSH tunnel.
output "web_service_url" {
  description = "The web Service's URL. Internal-ingress + IAM: not a public link."
  value       = google_cloud_run_v2_service.web.uri
}

output "set_anthropic_key" {
  description = "Populate the Claude key without putting it in Terraform state"
  value = join(" ", [
    "printf %s \"$ANTHROPIC_API_KEY\" | gcloud secrets versions add",
    google_secret_manager_secret.anthropic.secret_id,
    "--data-file=- --project ${var.project_id}",
  ])
}

output "chaos" {
  description = "Interrupt a Serverless Worker mid-research. Deliberately a terminal command, not an endpoint on the public page."
  value = join(" ", [
    "gcloud run worker-pools update ${google_cloud_run_v2_worker_pool.fleet.name}",
    "--instances 1 --region ${var.region} --project ${var.project_id}",
  ])
}

output "ssh" {
  description = "SSH to the Temporal VM"
  value       = "gcloud compute ssh ${google_compute_instance.temporal.name} --zone ${var.zone} --project ${var.project_id}"
}

output "bootstrap_log" {
  description = "Watch the VM finish provisioning Temporal + the Worker Deployment Version"
  value       = "gcloud compute ssh ${google_compute_instance.temporal.name} --zone ${var.zone} --project ${var.project_id} --command 'sudo tail -f /var/log/bb-bootstrap.log'"
}

output "temporal_ui_tunnel" {
  description = "Port-forward the Temporal Web UI to http://localhost:8233"
  value       = "gcloud compute ssh ${google_compute_instance.temporal.name} --zone ${var.zone} --project ${var.project_id} -- -N -L 8233:localhost:8233"
}

output "cli_tunnel" {
  description = "Port-forward the frontend so the local CLI and starter.py can reach it on localhost:7233"
  value       = "gcloud compute ssh ${google_compute_instance.temporal.name} --zone ${var.zone} --project ${var.project_id} -- -N -L 7233:localhost:7233"
}
