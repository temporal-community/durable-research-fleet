# Does this stack follow the knowledge base?

An honest audit of the implementation against `KNOWLEDGE_BASE.md`, section by
section. Verified 2026-07-28 against the live deployment, not read off the source.

**Summary: conforms on every requirement that is implementable today.** Three
items cannot be implemented because the thing the KB describes is not released
(§7.7), or not reachable through any available API (§7.3's `no_sync_quiet_ms`).
One KB section was found to be **wrong** and has been corrected (§7.6).

| § | Requirement | Status |
|---|---|---|
| 1 | Good-fit workload (bursty, scale-to-zero) | ✅ conforms |
| 2 | WCI drives scaling; sync-match failure is the trigger | ✅ verified live |
| 3 | Shutdown timeout > longest Activity | ✅ 20s > 5s |
| 3 | Heartbeats for long Activities | ✅ conforms |
| 4 | Slot isolation (limit slots) | ✅ 1 slot |
| 5 | Worker Versioning mandatory | ✅ enforced + tested |
| 6 | Cloud Run = pool resize, not per-invocation | ✅ conforms |
| 7.1 | Ordinary long-polling Worker | ✅ conforms |
| 7.2 | Backlog is the scaling signal | ✅ verified live |
| 7.3 | Scale-in can kill in-flight work | ⚠️ mitigated differently — see below |
| 7.4 | `no-sync` dynamic config | ⚠️ **KB incomplete** — needs `rate-based` too |
| 7.5 | Deployment steps | ✅ automated in Terraform |
| 7.6 | Self-hosted IAM prerequisites | ❌ **KB was wrong** — corrected |
| 7.7 | OpenTelemetry via `contrib.gcp` | ❌ not implemented (SDK module doesn't exist) |

---

## Where the KB was wrong, and is now fixed

### §7.6 — the IAM list was incomplete (the big one)

The KB said the invoker needs `run.workerPools.get` + `run.workerPools.update`
(`roles/run.developer`). That is **not sufficient**. It also needs
`iam.serviceAccounts.actAs` on the Worker Pool's **runtime** service account,
because a pool template names a runtime identity and resizing the pool is an
update that sets it.

Without it the WCI's `UpdateWorkerSetSize` Activity fails on every attempt and
the pool **silently never scales** — nothing in GCP reports an error. It appears
only in the Temporal server's own log. This cost several sessions of wrongly
assuming a configuration problem.

§7.6 now documents it; `terraform/iam.tf` grants it; `terraform test` and
`make verify` both assert it.

### §7.4 — the dynamic config was the Lambda configuration

The KB (following the published self-hosted docs) lists only
`workercontroller.scaling_algorithms.enabled: [no-sync]`. Cloud Run additionally
requires **`rate-based`**, matching §7.2's own description of "periodic
rate-based resizing". With `no-sync` alone, `create-version` fails:

```
Could not instantiate scaling algorithm with type 'rate-based'
```

Both are now enabled in `terraform/main.tf`'s `local.wci_dynamic_config`.

---

## Where we deviate deliberately

### §7.3 — `no_sync_quiet_ms` is not settable, so heartbeats do the work

The KB names `no_sync_quiet_ms` as the lever for keeping scale-in from killing
in-flight Activities, set "longer than your longest Activity". We cannot set it:
it exists only as a **protobuf field** on the Worker Deployment Version's
scaling-algorithm compute config (alongside `dispatch_rate`, `desired_count`,
`catch_up_rate`), and

- `temporal worker deployment create-version` on `main` exposes no flag for it
- it is not a dynamic config key (no `workercontroller.*` entry exists)

so it is reachable only through the Temporal Cloud UI's "Scaling and Lifecycle"
panel or the raw API. A Terraform variable for it would silently do nothing, so
there deliberately isn't one.

**What protects in-flight work instead**, which the KB also endorses:

- Activity Heartbeats every 1s (`activities.py`)
- `heartbeat_timeout=10s` (`workflows.py`) so a lost Worker is detected in ~10s
  rather than after the 30s `start_to_close_timeout`
- `graceful_shutdown_timeout=20s` plus SIGTERM handling (`runtime.py`)

Verified: SIGTERM mid-Activity lets the Activity **finish** inside the drain
window — history showed `ActivityTaskStarted 09:17:25 → ActivityTaskCompleted
09:17:30`, no timeout, no retry.

### §7.7 — observability is not implemented

The KB describes `temporalio.contrib.gcp.OpenTelemetryPlugin` with a collector
sidecar. **That module does not exist** — `temporalio` 1.30.0 (the latest release)
ships `contrib.aws`, `contrib.opentelemetry`, and others, but no `contrib.gcp`.
Same pattern as the CLI flags and the server: Cloud Run support trails Lambda.

This is the largest genuine gap. Closing it later means either the generic
`contrib.opentelemetry` plugin plus a manually-configured Google collector
sidecar, or waiting for `contrib.gcp`. Worth revisiting before any production use
— right now the only visibility into WCI behaviour is
`journalctl -u temporal` on the VM.

---

## Where the KB is followed, with the evidence

**§2 / §7.2 — the WCI and backlog-driven scaling.** Verified live: pool at 0, 5
Workflows queued, scaled **0 → 3 in ~20s**, all completed, back to **0** when
quiet. `make verify SCALE=1` reruns this.

**§4 — slot isolation.** `MAX_CONCURRENT_ACTIVITIES=1`. The KB's reason is
blast-radius (one Activity OOMing takes down others sharing the invocation);
there is a second reason it doesn't mention — the SDK default of 100 lets one
instance absorb an entire burst, so **no backlog forms and the WCI never scales**.
Verified: `--count 5` takes ~25s with the limit, ~5s without.

**§5 — Worker Versioning is mandatory.** `HelloWorkflow` declares `PINNED`;
`runtime.build_worker` always sets `use_worker_versioning=True`. Both are
asserted in `tests/test_serverless_contract.py`, including a check that no
Workflow in `workflows.py` lacks a versioning behavior.

**§3 — lifecycle ordering.** `graceful_shutdown_timeout` (20s) exceeds the
longest Activity (5s), satisfying the KB's "stop timeout > longest Activity
runtime" rule. Asserted in tests.

**§7.1 — ordinary long-polling Worker.** `runtime.run_worker` connects once and
polls until SIGTERM (`async with worker: await stop.wait()`). No Lambda-style
exit-after-batch. Asserted in tests.

**§7.5 — deployment steps.** All eight steps are Terraform + the VM bootstrap.
The one manual step is the first-run Task Queue registration (`make
register-queues`), needed because a pre-defined Version has no Task Queues until
a Worker polls — a detail the KB doesn't mention.

---

## How to re-check this

```bash
make test      # 28 pytest + 7 terraform test assertions, offline
make verify    # 15 live checks against the deployed stack
make verify SCALE=1   # adds the scale-from-zero cycle
```

Each Serverless-Workers requirement above has a corresponding test that names its
KB section, so a regression points back at the research rather than just failing.
