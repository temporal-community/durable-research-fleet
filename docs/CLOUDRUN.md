# Running Research Fleet on Google Cloud Run

The operating notes for the Cloud Run deployment. The README has the short path
(`make up`, `make verify SCALE=1`); this page holds the details you need when something
needs changing or goes wrong. For the Amazon Bedrock AgentCore target, see
[AGENTCORE.md](AGENTCORE.md). To choose between them, see "Choose your platform" in the
[README](../README.md#-choose-your-platform).

The hard rules (the bugs that must not come back) live in [../AGENTS.md](../AGENTS.md).
The manual, step-by-step version of the deploy is [TUTORIAL.md](TUTORIAL.md).

## Opening the console

The Cloud Run web Service is intentionally **internal-only**. Opening its `run.app` URL from
a public browser should fail; that is the security boundary, not a broken deployment.
(AGENTS.md gate #12 explains why it is not made public.)

Serve the same workbench locally while it drives the real deployed Temporal and Cloud Run fleet:

```bash
# Terminal 1 — leave running
make tunnel PROJECT=your-project-id

# Terminal 2
make web-local
```

Then open **http://localhost:8000**. Both tunnels auto-reconnect, so a VPN re-handshake or a
laptop sleeping costs about five seconds instead of the run.

### Ports

Every local port is overridable, which matters if you already have a Temporal dev server or
another app running. `web-local` follows `LOCAL_FRONTEND_PORT`, so pass the same value to both
and nothing else needs setting:

```bash
make tunnel PROJECT=your-project-id LOCAL_FRONTEND_PORT=7433
make web-local LOCAL_FRONTEND_PORT=7433 WEB_PORT=8100
```

### If SSH times out

A timeout, rather than a refusal, means the SSH firewall rule is pinned to a public IP you no
longer have. See `ssh_source_ranges` in `terraform/variables.tf`.

## Shipping new code

Pushing the same image tag does **not** redeploy the Worker Pool: Cloud Run pins the image
digest per revision, and Terraform sees no change for the pool. After rebuilding the image,
force the Worker Pool onto the new digest. The exact command, and the other deployment
invariants, are in [AGENTS.md](../AGENTS.md) gate #14.

Do not deploy a new `BUILD_ID` while a Workflow is awaiting review: parked executions are
pinned to the Version they started on.

## Tearing it down

```bash
make down PROJECT=your-project-id
```

A partial teardown is normal: Cloud Run can hold a `serverless-ipv4-*` address reservation in
the subnet and releases it on its own schedule. Everything else is gone; only the subnet and
VPC remain, and they cost nothing. Re-run `make down` later to finish. Check with
`gcloud compute addresses list --project your-project-id`.

## Known limits

- The CLI flags and the server this needs are unreleased (AGENTS.md "Deployment gates"), so a
  fresh checkout depends on `make cli`, which builds a main-branch CLI.
- The web tier is reachable only through the tunnel, by design.
- Recreating the stack after a teardown can hit GCP's 37-day custom-role id reservation
  (`role_id (workerPoolScaler) ... marked for deletion`). Set `SCALER_ROLE_SUFFIX` to get a
  fresh id; see `terraform/variables.tf`.
- `egress = "PRIVATE_RANGES_ONLY"` on the pool is load-bearing: switching it to `ALL_TRAFFIC`
  would route model calls into a subnet with no NAT and break every research Activity while
  the token-free smoke test kept passing.
