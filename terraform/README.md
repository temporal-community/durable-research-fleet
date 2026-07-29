# The stack

One `terraform apply` stands up everything needed to run Research Fleet on a Cloud
Run Worker Pool scaled by a self-hosted Temporal Service. One `destroy` removes it.

From the repo root:

```bash
make up      # build the CLI, create the registry, push the image, apply
make down    # destroy
```

`make up` sequences the parts Terraform can't do itself — Terraform manages cloud
resources, not container images or Go builds.

## What gets created

| File | Resources |
|---|---|
| `main.tf` | provider, operator-IP detection, API enablement |
| `network.tf` | VPC, subnet, static internal IP, two firewall rules |
| `iam.tf` | three service accounts and the impersonation chain |
| `artifacts.tf` | Artifact Registry repo, bootstrap bucket, CLI object |
| `vm.tf` | the Temporal VM + its bootstrap |
| `workerpool.tf` | the Cloud Run Worker Pool |

### The three identities

Deliberately separate, so the thing that can *scale* the pool cannot *run* code:

- **invoker** — reads and resizes the Worker Pool (`roles/run.developer`, **plus
  `actAs` on the runtime SA** — see below). Runs nothing itself.
- **temporal-vm** — the Temporal Service. Impersonates the invoker via metadata ADC.
- **worker-rt** — what the pool's containers run as. Can pull images, nothing more.

### Why there's no tunnel

The VM holds a static internal IP; the pool uses **Direct VPC egress** into the
same subnet and dials it privately. The frontend is firewalled to the subnet CIDR
and is never reachable from the internet. SSH is scoped to your detected public IP
(override with `ssh_source_ranges`, or `[]` to disable SSH).

## Four things that will bite you

**1. It requires an unreleased Temporal server.** The Cloud Run compute provider
needs the server bundled in the `main` branch of `temporalio/cli`
(1.32.0-158.0+). Released builds — including `temporalio/server:1.31.2` — accept
the config keys and then fail:

```
Could not instantiate scaling algorithm with type 'rate-based'
```

`make cli` builds the right binary; Terraform ships it to the VM via GCS because
there is no public image or release asset to fetch.

**2. `rate-based` must be enabled, and the docs don't say so.** The published
self-hosted setup page lists only `no-sync` — that's the Lambda configuration.
Cloud Run needs both (see `../docs/KNOWLEDGE_BASE.md` §7.2).

It is configured in `main.tf`'s `local.wci_dynamic_config`, which is rendered
into `--dynamic-config-value` launch flags. It is deliberately **not** read from
`../selfhosted/dynamicconfig/wci.yaml`: `temporal server start-dev` has no
`--dynamic-config-file` flag, so a YAML file handed to it would be silently
ignored. That file is for the compose stack only, which runs the full server and
does read a file.

**3. `iamcredentials.googleapis.com` must be on.** Without it, `create-version`
fails with `iam.serviceAccounts.getAccessToken` denied — which reads like a
missing IAM binding rather than a disabled API. `main.tf` enables it.

**4. IAM propagation lags ~60s.** A fresh `apply` can have the VM's bootstrap hit
`getAccessToken denied` on first boot. The bootstrap script is idempotent and
re-runs on reboot; `sudo systemctl restart google-startup-scripts` re-runs it
without one.

## Tuning knobs that matter

| Variable | Default | Why it matters |
|---|---|---|
| `max_concurrent_activities` | `1` | Activity slots per instance. **Leave at 1 unless you know why you're raising it.** The SDK default is 100, which lets one instance swallow a whole burst so no Task Queue backlog forms and the WCI never scales — the demo silently does nothing. Also gives KB §4 slot isolation. |
| `graceful_shutdown_seconds` | `20` | Drain window after SIGTERM. Keep below Cloud Run's termination grace period or the process is SIGKILLed mid-drain. |
| `max_instances` | `10` | Cost guard, applied as `workercontroller.maxInstances`. Best-effort — see the caveat in `main.tf`. |
| `worker_cpu` / `worker_memory` | `1` / `512Mi` | Sized for one Activity slot. Raise together with the slot count. |

### `no_sync_quiet_ms` is not settable here

KB §7.3 names it as the lever for stopping scale-in from killing in-flight
Activities, but there is deliberately no variable for it: it is a field on the
Worker Deployment Version's scaling-algorithm compute config (it appears in the
server binary only as a protobuf field name), the CLI on `main` exposes no flag
for it, and it is not a dynamic config key. It is reachable only via the Temporal
Cloud UI's "Scaling and Lifecycle" panel or the raw API.

What protects in-flight work on this path instead is **Activity Heartbeats plus a
`heartbeat_timeout`** (`activities.py`, `workflows.py`) and the Worker's graceful
shutdown. Verified: SIGTERM mid-Activity lets the Activity finish inside the
drain window, with no spurious timeout or retry.

## Instance count is not managed here

Terraform owns the pool's *shape* (image, env, networking) but not its instance
count — that belongs to the Worker Controller. `workerpool.tf` has:

```hcl
lifecycle {
  ignore_changes = [scaling[0].manual_instance_count]
}
```

Without it, every `plan` would try to drag a scaled-up pool back to 0.

## Scale-from-zero: working, and the one binding that makes it work

Verified end to end on 2026-07-28: pool at 0 instances, 5 Workflows queued, the
Worker Controller scaled the pool **0 → 3 within 20s**, all Workflows completed,
and the pool returned to **0** once the queue went quiet.

The cold-start cost of scale-to-zero, measured the same day: the first Workflow
against an **empty** pool took **~70s** end to end (WCI notices the backlog →
resizes the pool → Cloud Run boots an instance → the 5s Activity runs), and the
next Workflow against the now-**warm** pool took **~6.8s**. That gap is the
trade-off you accept in exchange for paying nothing while idle.

Getting there needed one IAM binding that neither KB §7.6 nor the upstream
Terraform module grants — `iam.tf`'s `invoker_acts_as_worker_rt`:

```hcl
resource "google_service_account_iam_member" "invoker_acts_as_worker_rt" {
  service_account_id = google_service_account.worker_rt.name
  role               = "roles/iam.serviceAccountUser"   # iam.serviceAccounts.actAs
  member             = "serviceAccount:${google_service_account.invoker.email}"
}
```

`roles/run.developer` alone is not enough. A Worker Pool template names a runtime
service account, so resizing it is an update that sets that identity, and Cloud
Run requires `actAs` on it. Without the binding, `UpdateWorkerSetSize` fails every
attempt and **the pool silently never scales — GCP reports nothing.** The only
place it shows up is the Temporal server's own log:

```bash
gcloud compute ssh research-fleet-temporal --zone us-central1-a \
  --command 'sudo journalctl -u temporal | grep UpdateWorkerSetSize'
```

### One-time bootstrap: the Version needs its Task Queues

A pre-defined Worker Deployment Version has a compute config but **no Task
Queues** until a Worker actually polls, and the WCI needs the Task Queue
association to know what to watch. So after the first `make up`, run the pool once:

```bash
gcloud run worker-pools update research-fleet-worker-pool --instances 1 --region us-central1
# wait ~40s, confirm Task Queues now appear:
#   temporal worker deployment describe-version --deployment-name research-fleet --build-id v1
gcloud run worker-pools update research-fleet-worker-pool --instances 0 --region us-central1
```

From then on the WCI handles it. Watch with `make status`.

## After apply

```bash
make bootstrap-log   # watch the VM register the Worker Deployment Version
make tunnel          # forward the frontend to localhost:7233
make ui              # Temporal Web UI at http://localhost:8233
make status          # pool instance count + VM state
```

With `make tunnel` running, the CLI works unchanged:

```bash
.venv/bin/python starter.py --watch
```

## Migrating from the hand-built setup

`imports.tf` can adopt the three resources whose names match (notably the
Artifact Registry repo — recreating it would delete your pushed image), but it is
**off by default** so a clean project can apply. Enable it only for the migration:

```bash
make adopt   # apply with adopt_existing=true
```

Resources whose names don't match can't be adopted — delete those by hand first.

This project's own migration is **done**: the hand-built environment was torn out
on 2026-07-28 and the entire stack rebuilt by Terraform from an empty project
(26 resources), so `adopt_existing` is no longer needed here.

## Testing

Three layers, because each catches a different class of failure:

```bash
make test      # 28 pytest + 7 terraform test assertions — offline, no cloud
make verify    # 15 checks against the LIVE stack
make verify SCALE=1   # adds the real scale-from-zero cycle (~4 min)
```

`make test` proves the config and app are right. It cannot prove the deployed
stack scales — the `actAs` bug passed every offline check while the pool sat at 0
forever. That's what `make verify` is for; it asserts the IAM bindings and Version
state whose absence fails silently, and `SCALE=1` actually queues Workflows
against an empty pool and watches the count move.

Each `terraform test` assertion corresponds to a bug this stack has already had.

## Teardown has a known GCP wrinkle

`make down` destroys everything, but the **subnet and VPC may survive the first
pass**. Cloud Run's Direct VPC egress reserves an internal address
(`serverless-ipv4-*`) in the subnet and, after the pool is deleted, keeps it
`RESERVED` behind a `serverless.googleapis.com` addressReservation. That address
cannot be deleted by hand:

```
ERROR: The address resource '...' is already being used by
'//serverless.googleapis.com/.../addressReservations/serverless-ipv4-...'
```

GCP releases it asynchronously on its own schedule — **observed at roughly 40
minutes** on this project. `make down` prints an explanation and is safe to
re-run; the second pass removes the subnet and VPC. A leftover subnet and VPC
cost nothing.

Also worth knowing: the pool sets `deletion_protection = false`. The provider
defaults it to **true**, which makes `terraform destroy` fail *partway* — after
other resources are already gone — leaving a half-torn stack to clean up by hand.

## Cost

An `e2-small` is roughly $13/month; the pool bills only while instances run;
Artifact Registry and the bootstrap bucket are cents. `make down` removes all of
it. The bucket is `force_destroy = true` so destroy doesn't stall on the object.
