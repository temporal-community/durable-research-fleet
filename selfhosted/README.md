# Self-hosted Temporal with the Worker Controller enabled

> ### Use `../terraform` for the real thing
>
> This compose stack runs the **released** `temporalio/server:1.31.2`, and that
> version **cannot actually drive Cloud Run**. It accepts all three
> `workercontroller.*` keys — including `gcp-cloud-run` — and then fails at
> `create-version` with:
>
> ```
> Could not instantiate scaling algorithm with type 'rate-based'
> ```
>
> The Cloud Run provider needs the *unreleased* server bundled in the `main`
> branch of `temporalio/cli` (1.32.0-158.0+), which is not published as a Docker
> image. `../terraform` therefore runs that binary directly on a GCE VM, and is
> the path that works end to end.
>
> Keep using this stack for **local development** — running Workflows, the Web
> UI, poking at Worker Deployment Versions. Just don't expect the WCI to scale a
> pool from it.

This stack exists to run a self-hosted Temporal locally with the Worker
Controller *configured*. The Temporal Cloud Pre-release gate is Cloud-side only,
so self-hosting sidesteps it — but you still need a server new enough to
implement the Cloud Run provider (see the box above).

```bash
docker compose up -d --wait     # --wait blocks until every healthcheck passes
```

Hardened for repeated use: every long-running service has `restart:
unless-stopped`, capped json-file logging (so days of uptime can't fill the disk),
a memory limit, and a 30s `stop_grace_period` so in-flight Workflow Tasks finish
instead of being SIGKILLed. The schema job retries a not-quite-ready Postgres, and
the UI waits for `service_healthy` rather than `service_started` so it doesn't
boot into a connection error. Verified from an empty volume: `down -v` then
`up -d --wait` reaches all-healthy.

| Service | Port | Notes |
|---|---|---|
| `temporal` (frontend, gRPC) | **7236** | 7233 is left free for any other Temporal you run |
| `temporal-ui` | **8236** | http://localhost:8236 |
| `postgresql` | — | internal only, data in the `pgdata` volume |
| `schema` | — | one-shot job, exits 0 |

Override with `FRONTEND_PORT` / `UI_PORT`.

First run only — the `default` Namespace isn't created for you (see gotcha #2):

```bash
temporal operator namespace create -n default --address localhost:7236 --retention 24h
```

Then, from the repo root:

```bash
temporal worker deployment set-current-version \
  --address localhost:7236 --namespace default \
  --deployment-name research-fleet --build-id local --yes

TEMPORAL_ADDRESS=localhost:7236 .venv/bin/python worker_local.py     # terminal 2
TEMPORAL_ADDRESS=localhost:7236 .venv/bin/python starter.py --watch  # terminal 3
```

Verified working: one Workflow completes in ~5.3s; `starter.py --count 5 --watch`
takes ~25s because each Worker takes one Activity at a time (that serialization is
deliberate — it's what creates the backlog the Worker Controller scales on).

## Verifying the WCI actually turned on

```bash
docker compose logs temporal | grep workercontroller
```

You want all three keys, and `gcp-cloud-run` among the providers:

```
dynamic config changed for the key: workercontroller.enabled ... value: true
dynamic config changed for the key: workercontroller.compute_providers.enabled ... value: [gcp-cloud-run]
dynamic config changed for the key: workercontroller.scaling_algorithms.enabled ... value: [no-sync rate-based]
```

Unlike the VM, this stack runs the **full** server, which really does read a
dynamic config file — that's why `wci.yaml` exists and is mounted here. The VM
runs `start-dev`, which has no `--dynamic-config-file` flag, so `../terraform`
renders `--dynamic-config-value` launch flags from `../terraform/main.tf` instead.

`dynamicconfig/wci.yaml` is watched live — edit it and the server picks the
change up without a restart.

## Four gotchas this compose file already works around

1. **`temporalio/auto-setup` is only published up to 1.29.7**, below the 1.31.0
   minimum. That's why this runs Postgres + a `admin-tools:1.31.2` schema job +
   `temporalio/server:1.31.2` instead of the usual single service.
2. **`temporalio/server` does not create the `default` Namespace.** auto-setup
   did that. Register it manually once (command above), or every client call
   fails with a namespace-not-found error.
3. **`admin-tools` is Alpine — there is no `/bin/bash`.** The schema job's
   entrypoint is `/bin/sh`.
4. **`temporalio/server` ships only `temporal-server`, no `temporal` CLI**, so
   the healthcheck can't call `temporal operator cluster health`. Probing the
   port instead has two further traps: the frontend binds the **container IP,
   not loopback** (so `127.0.0.1:7233` refuses the connection even while the
   server is happily SERVING), and BusyBox `nc` has no usable `-z`. Hence
   `nc -w 1 $(hostname -i) 7233 </dev/null`.

## GCP credentials

The server impersonates the invoker service account to resize the Worker Pool, so
it needs Application Default Credentials:

```bash
gcloud auth application-default login
```

The compose file mounts `~/.config/gcloud/application_default_credentials.json`
read-only at `/gcp/adc.json` and points `GOOGLE_APPLICATION_CREDENTIALS` at it.
Your user account also needs `roles/iam.serviceAccountTokenCreator` on the
invoker SA — that's what `impersonator_user_emails` in `../terraform` grants.

## Still unsolved: Cloud Run → Temporal reachability

The WCI can reach *out* to the Cloud Run admin API from your laptop just fine.
The reverse does not work: Worker Pool instances must dial the Temporal frontend,
and `localhost:7236` is not reachable from Cloud Run.

**Cloudflare Tunnel cannot solve this.** It does not proxy gRPC over public
hostnames on any plan, and the free plan strips the `content-type:
application/grpc` header. Temporal's frontend is gRPC.

Workable options — see `../docs/TUTORIAL.md`:

| Option | Trade-off |
|---|---|
| Raw TCP tunnel (`ngrok tcp`) | Simplest; gRPC unaffected since nothing parses HTTP. Exposes an unauthenticated frontend publicly — fine for a short demo, not otherwise. |
| `cloudflared` sidecar in the pool + Cloudflare Access | Free and not publicly exposed, but the most moving parts. |
| Run this stack on a GCE VM | Most production-faithful, firewall-scoped, no tunnel. Gives up "everything local". |

## Teardown

```bash
docker compose down        # keeps the pgdata volume
docker compose down -v     # also deletes all Workflow history
```
