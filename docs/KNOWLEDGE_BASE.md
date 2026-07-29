# Serverless Workers — Knowledge Base

> **New to this?** This file is the deep research base — dense and
> reference-first. For a guided introduction, start at [`../learn/`](../learn/README.md):
> 14 short cards from "what is Temporal" to "why the pool scales".

Everything gathered on Temporal Serverless Workers across docs (official +
preview), the official blog, the `temporal-serverless-no-roads` Lambda demo
repo, a docs-vs-demo consistency audit, and devrel/demo ideation. This is the
consolidated "second brain" for the project — read this before the
`TUTORIAL.md` if you want the *why*, not just the *how*.

**Status at time of writing:** Serverless Workers are in **Pre-release**.
Request access via a Temporal Cloud support ticket or your account team.
APIs are experimental and may change in backwards-incompatible ways.
GA support currently: Go, Python, TypeScript SDKs, on AWS Lambda (shipped)
and GCP Cloud Run (in preview docs, rolling out); Java, .NET, and further
providers are on the roadmap per the official launch blog.

---

## Table of Contents
1. [What Are Serverless Workers](#1-what-are-serverless-workers)
2. [Architecture: The Worker Controller Instance (WCI)](#2-architecture-the-worker-controller-instance-wci)
3. [Worker Lifecycle](#3-worker-lifecycle)
4. [Failure Handling](#4-failure-handling)
5. [Worker Versioning — Mandatory](#5-worker-versioning--mandatory)
6. [Compute Providers Overview](#6-compute-providers-overview)
7. [GCP Cloud Run — Deep Dive](#7-gcp-cloud-run--deep-dive)
8. [AWS Lambda — Deep Dive](#8-aws-lambda--deep-dive)
9. [Cloud Run vs Lambda — Side by Side](#9-cloud-run-vs-lambda--side-by-side)
10. [Reference Demo: temporal-serverless-no-roads](#10-reference-demo-temporal-serverless-no-roads)
11. [Porting That Demo to Cloud Run](#11-porting-that-demo-to-cloud-run)
12. [Docs-vs-Demo Consistency Audit](#12-docs-vs-demo-consistency-audit)
13. [DevRel / Demo Ideas](#13-devrel--demo-ideas)
14. [This Project: Research Fleet POC](#14-this-project-research-fleet)
15. [Glossary](#15-glossary)
16. [Source List](#16-source-list)
17. [Open Questions / Watch List](#17-open-questions--watch-list)

---

## 1. What Are Serverless Workers

A Serverless Worker is a standard Temporal Worker — same SDK, same
Workflow/Activity registration — whose **lifecycle is managed by Temporal
instead of by you**. Temporal decides when a Worker instance needs to exist
and tells the compute provider to start or scale it, rather than you running
an always-on process.

### Why it exists
| Benefit | Mechanism |
|---|---|
| Less operational overhead | No always-on infra or hand-tuned autoscaling policies |
| Faster to get started | Deploying a Worker ≈ deploying a function/container |
| Automatic scaling | Provider scales instances based on real Task Queue signals |
| Pay-per-use | No idle compute cost during quiet periods |

### Good fit
- Bursty / event-driven workloads (orders, notifications, webhooks)
- Low or intermittent traffic
- Teams already standardized on serverless compute
- Multi-tenant platforms that don't want a dedicated Worker fleet per tenant

### Poor fit
- Long-running, uninterruptible Activities on providers with hard time caps (Lambda's 15 min)
- Sustained high-volume Task Queues — dedicated long-lived Workers are usually cheaper
- Anything needing a persistent connection an invocation-based model can't hold

**Key clarification:** Workflow *duration* is never the constraint — a
Workflow can span any number of Worker invocations over any length of time.
The only real constraint is **Activity** duration vs. the provider's
execution model.

---

## 2. Architecture: The Worker Controller Instance (WCI)

The WCI is a **system Workflow** — one instance runs per Worker Deployment
Version that has a compute provider configured, in the same Namespace as
your Worker Deployment.

```
temporal workflow list --namespace <NS> \
  --query 'TemporalNamespaceDivision = "TemporalWorkerControllerInstance"'
```
WCI Workflow IDs: `temporal-sys-worker-controller-instance:<deployment-name>:<build-id>`

### The two triggers
1. **Sync match failure** (primary, low-latency) — Matching Service tries to
   hand a Task directly to an already-polling Worker. If none is free, the
   match fails and a signal is **pushed** to the WCI immediately — no timer
   wait. This is what keeps scale-out latency low.
2. **Task Queue backlog** (secondary) — the WCI also watches for pending
   Tasks with insufficient Workers and adds capacity to burn down backlog.

> **Common simplification to watch for:** marketing copy and even the
> official launch blog tend to describe this loosely as "Temporal watches
> backlog and invokes your function." The more precise mechanism (per the
> encyclopedia docs) is that sync match failure is the *primary* trigger —
> it fires on the very first task that finds no free poller, before any
> backlog has built up. Backlog-based resizing is the secondary, rate-based
> mechanism layered on top.

### End-to-end flow
1. Task submitted → 2. Matching attempts sync match → 3. If available, routed
directly → 4. If not, signal to WCI → 5. WCI invokes/scales the compute
provider → 6. Worker starts, connects, polls → 7. Worker processes Tasks.

Each invocation/instance is independent — no connection reuse or shared state.

### Mixing with long-lived Workers
Serverless Workers can share a Task Queue with long-lived Workers, acting as
spillover capacity (they only spin up on sync-match failure).

> ⚠️ Don't enable dynamic scaling on a long-lived fleet sharing a queue with
> Serverless Workers — the two scaling systems can't coordinate and can both
> scale up for the same Tasks simultaneously.

---

## 3. Worker Lifecycle

Three phases per invocation: **init** (connect) → **work** (poll + process)
→ **shutdown** (stop polling, drain in-flight Tasks, run shutdown hooks).
Shutdown begins *before* the hard invocation deadline so the process exits
cleanly ahead of forced termination.

### Tuning three settings together for long Activities
| Setting | Rule |
|---|---|
| Worker stop timeout | > longest Activity runtime |
| Shutdown deadline buffer | > stop timeout + shutdown hook time |
| Invocation deadline (provider-side) | > longest Activity runtime + shutdown deadline buffer |

Example: longest Activity 5 min, shutdown hooks 3s → stop timeout > 5 min;
buffer > 303s; invocation deadline ≥ 10 min 3s.

If your longest Activity exceeds roughly half the max invocation deadline,
this math gets infeasible — use **Activity Heartbeats** so a retried
Activity resumes from last progress instead of restarting cold.

Getting the knobs backwards has real consequences: raising only the
shutdown buffer stops polling earlier but doesn't extend in-flight Task
time; raising only the stop timeout risks the provider killing the process
before it fully elapses, skipping shutdown hooks entirely.

---

## 4. Failure Handling

| Scenario | What happens |
|---|---|
| Worker crash (OOM, unhandled exception) | Standard Temporal retries — Activity Timeout fires, retried on a different invocation, no manual intervention |
| Provider concurrency limit hit | Further WCI invocations fail; Tasks stay queued (no data loss); processing slows until concurrency frees up |
| Resource exhaustion across Activity slots | One Activity OOMing can take down others in the *same* invocation, since one invocation may run multiple Activity slots by default. Mitigate by splitting Workflow/Activity Workers into separate compute functions, or limiting slots to 1 for isolation |

---

## 5. Worker Versioning — Mandatory

Serverless Workers **require** Worker Versioning. Every Workflow must
declare a versioning behavior:
- **PINNED** — stays on its original Worker Deployment Version until completion
- **AUTO_UPGRADE** — moves to the new current version at its next Workflow Task

```python
from temporalio import workflow
from temporalio.common import VersioningBehavior

@workflow.defn(versioning_behavior=VersioningBehavior.PINNED)
class MyWorkflow:
    @workflow.run
    async def run(self, input: str) -> str: ...
```

Or set a Worker-level default via `default_versioning_behavior` /
`DefaultVersioningBehavior` in the deployment config.

A **compute provider** is configuration attached to a Worker Deployment
Version telling Temporal *how* to invoke your Worker. Only Serverless
Workers need this — traditional long-lived Workers manage their own
lifecycle.

---

## 6. Compute Providers Overview

| Provider | Status | Mechanism |
|---|---|---|
| AWS Lambda | Shipped | Temporal assumes an IAM role in your account to call `lambda:InvokeFunction` |
| GCP Cloud Run | Preview docs, rolling out | Temporal impersonates a GCP service account ("invoker") to resize a **Worker Pool** via the Cloud Run admin API |
| Java, .NET SDKs; further providers | Roadmap, per official launch blog | — |

---

## 7. GCP Cloud Run — Deep Dive

### 7.1 The core mental model shift

| | AWS Lambda | GCP Cloud Run |
|---|---|---|
| Unit of compute | A **function invocation** — starts fresh, runs, exits | A **Worker Pool**: long-lived instances that poll continuously |
| What WCI controls | Whether to invoke the function at all | **Instance count** of the pool (0→N) |
| Worker code style | Init → poll → process → exit, each time | Ordinary long-running Worker code — connect once, poll forever |
| Activity duration limit | Hard 15-min cap | No equivalent hard cap — Cloud Run is explicitly cited (in the general encyclopedia docs) as a provider with longer limits, because instances are long-lived |
| Versioning-to-compute mapping | Pin to an immutable, *versioned* Lambda ARN; an unqualified ARN silently tracks `$LATEST` and is dangerous for Pinned workflows | Cloud Run's own encyclopedia page marks its "Worker Versioning" section "content coming soon" at time of writing; the deployment guide instead ties a version to a specific Worker Pool + build tag matched by deployment name/build ID |

**Bottom line:** on Cloud Run you are *not* writing per-invocation code. A
Cloud Run Serverless Worker is the same kind of Worker you'd run on a VM or
in Kubernetes — what Cloud Run + the WCI add is automatic pool sizing
(including scale-to-zero), not a per-task cold-start-and-die model.

### 7.2 Autoscaling mechanics

Two combined mechanisms:
1. **Immediate reaction** on sync match failure — absorbs bursts without waiting for an evaluation cycle
2. **Periodic rate-based resizing** — measures Task arrival rate vs. per-Worker processing rate, computes a target instance count

The WCI targets **80% utilization by default**, not 100% — headroom to
absorb newly arriving Tasks immediately instead of queuing them. If backlog
has already built, it adds instances on top of the baseline to burn it down
faster. Applied via the Cloud Run admin API, bounded by configured min/max.

**Scale-out** grows *ahead* of demand — a new instance takes real time to
start and connect, so provisioning early avoids backlog growth during that
startup window.

**Scale-in** is deliberately conservative: holds capacity while sync match
failures are still occurring, applies a **cooldown** before reducing, and
can scale to zero when idle.

### 7.3 The single biggest Cloud Run gotcha: pool-level scale-in

> Scale-in decisions are made **at the pool level**, not per-instance. The
> WCI does not track how long a given instance has run or whether it's
> mid-Activity — it reacts to aggregate queue signals. **The instance Cloud
> Run stops may be one that's still executing work.**

Mitigation lever: **`no_sync_quiet_ms`** — how long the queue must stay
quiet (no sync match failures) before the WCI lowers the target instance
count. Set this **longer than your longest-running Activity**. Combine with
**Activity Heartbeats** as a safety net so an interrupted Activity resumes
from last progress rather than restarting.

### 7.4 Self-hosted config (the `no-sync` scaling algorithm)

```yaml
workercontroller.enabled:
  - value: true
workercontroller.compute_providers.enabled:
  - value: [gcp-cloud-run]
workercontroller.scaling_algorithms.enabled:
  - value: [no-sync]
```
The `no-sync` name directly corresponds to `no_sync_quiet_ms` — the
algorithm driving Cloud Run's conservative, sync-match-failure-based
scale-in. Can be scoped to one Namespace via a `constraints.namespace`
clause. Config changes apply live, no restart needed.

### 7.5 Deployment steps (condensed — full version + code in `TUTORIAL.md`)

1. Prerequisites: Temporal Cloud account with GCP-hosted Namespace, or
   self-hosted v1.31.0+; every Workflow has a versioning behavior; GCP
   project with Cloud Run + Artifact Registry enabled
2. Write Worker code — an ordinary long-running Worker with
   `WorkerDeploymentConfig` set
3. Containerize, push to Artifact Registry
4. Create the Worker Pool at `--instances 0`, with a **runtime service
   account** and secrets mounted via `--set-secrets`
5. (Cloud only) Deploy Temporal's Terraform module to create the **invoker
   service account** Temporal impersonates
6. Create the Worker Deployment Version, pointing at the Worker Pool +
   invoker service account; optionally set `no_sync_quiet_ms` under
   "Scaling and Lifecycle"
7. Set the version current
8. Verify: start a Workflow, check the Temporal UI + `gcloud run
   worker-pools logs read` (expect **no logs** until an instance actually starts)

### 7.6 Self-hosted-only prerequisites
- Network reachability (Direct VPC egress / Serverless VPC Access connector) from Cloud Run to the Temporal Service frontend
- Enable WCI via dynamic config (§7.4)
- Give the Temporal Service a GCP identity (ADC on GCE/GKE, or Workload Identity Federation outside GCP)
- Create the invoker service account: Temporal's GCP identity gets
  `roles/iam.serviceAccountTokenCreator` on it; the invoker gets a Cloud Run
  role with at least `run.workerPools.get` + `run.workerPools.update`
  (`roles/run.developer` covers both)
- **⚠️ The docs' IAM list above is INCOMPLETE — verified 2026-07-28.** The invoker
  also needs **`iam.serviceAccounts.actAs` on the Worker Pool's *runtime*
  service account** (`roles/iam.serviceAccountUser`). A Worker Pool template
  names a runtime identity, so resizing the pool is an update that sets that
  identity, and Cloud Run enforces `actAs`. Without it the WCI's
  `UpdateWorkerSetSize` Activity fails on every attempt with:

  ```
  failed to update worker pool ".../research-fleet-worker-pool": rpc error:
  code = PermissionDenied desc = Permission 'iam.serviceaccounts.actAs'
  denied on service account <runtime-sa>@<project>.iam.gserviceaccount.com
  ```

  This is the failure mode to know about: the pool simply never scales, and
  **nothing in GCP reports an error** — it surfaces only in the Temporal server's
  own debug log (`--log-level debug`, grep `UpdateWorkerSetSize`). The upstream
  `terraform-modules//serverless-workers/gcp/cloud-run` module does not grant it
  either. Adding the binding took the pool from permanently 0 to scaling 0→3
  within 20s.

### 7.7 Observability (Python — `temporalio.contrib.gcp.OpenTelemetryPlugin`)
Purpose-built defaults: OTLP gRPC export to a `localhost:4317` collector
sidecar, `service.name` sourced from the Cloud Run `CLOUD_RUN_WORKER_POOL`
env var, a replay-safe tracer provider, Core metrics exported every 60s. Run
Google's Built OpenTelemetry Collector as a second container — it
auto-detects the Cloud Run resource and routes metrics to Google Managed
Prometheus, traces to the Cloud Trace API. Notable config detail: the
metrics pipeline intentionally has **no batch processor**, so a
shutdown-time export can't collide with a periodic export on the same
Managed Prometheus series; a `transform/collision` processor renames
attributes that would otherwise clash with Cloud Monitoring's reserved labels.

---

## 8. AWS Lambda — Deep Dive

(Confirmed directly against the official `production-deployment/worker-deployments/serverless-workers/aws-lambda` docs page.)

### 8.1 Worker code
Uses a language-specific serverless Worker package:
- Go: `go.temporal.io/sdk/contrib/aws/lambdaworker`, `lambdaworker.RunWorker(...)`
- Python: `temporalio.contrib.aws.lambda_worker`, `run_worker(...)` returns a Lambda handler
- TypeScript: `runWorker(...)`, using a pre-bundled `workflowBundle` (not `workflowsPath`) to avoid webpack overhead on cold starts

Each requires a `WorkerDeploymentVersion` (deployment name + build ID) and a
per-Workflow or default versioning behavior, same as Cloud Run.

### 8.2 Deploy steps
1. Write Worker code (above)
2. Build/package (cross-compile Go for `linux/amd64`; zip Python deps + code; `npm install --omit=dev` + zip for TS)
3. `aws lambda create-function` with an **execution role** (separate from
   the invocation role Temporal uses — must have at least
   `AWSLambdaBasicExecutionRole`), `--timeout` (invocation deadline in
   seconds — the docs' own example uses 600s) and `--memory-size` (example:
   256 MB)
4. **Configure IAM for Temporal invocation (Cloud only)** — deploy a
   Temporal-provided CloudFormation template that creates a role Temporal
   assumes via `sts:AssumeRole`, gated by an **External ID condition**
   specifically to prevent confused-deputy attacks; grants
   `lambda:InvokeFunction` + `lambda:GetFunction` on your function ARN(s)
5. Create the Worker Deployment Version (UI or CLI) with provider type
   `aws-lambda`, the Lambda ARN, the invocation role ARN, and the External ID
6. Set the version current (automatic if created via UI)
7. Verify: start a Workflow, check Temporal UI + CloudWatch Logs
   (`/aws/lambda/<function-name>`)

### 8.3 Lambda versioning best practice (explicitly called out in docs)
> Create a 1-to-1 mapping between each Build ID in Worker code and a Lambda
> function version. If using an unversioned Lambda (pointing at `$LATEST`),
> do not change the Build ID in Worker code without also creating a new
> Worker Deployment Version.

The stricter, production-grade version of this rule (from the general
encyclopedia docs): map each Worker Deployment Version to exactly one
**immutable, numbered** Lambda function version, and use the **qualified
ARN** (e.g. `...:function:my-worker:5`). An unqualified ARN tracking
`$LATEST` risks non-determinism errors for in-flight Workflows — even ones
marked Pinned — if replay-unsafe code gets deployed underneath them. Docs
explicitly note the unqualified/simpler path is acceptable **for
development or non-critical workloads**.

---

## 9. Cloud Run vs Lambda — Side by Side

| | AWS Lambda | GCP Cloud Run |
|---|---|---|
| Compute unit | Per-invocation function | Long-lived Worker Pool instances |
| Scaling lever | Invoke / don't invoke | Resize pool instance count (0→N) |
| Code shape | Special serverless-Worker SDK package | Ordinary `Worker` construction |
| Trust mechanism (Cloud) | AWS STS AssumeRole + External ID (CloudFormation) | GCP service account impersonation (Terraform module) |
| Runtime identity | Lambda execution role | Worker Pool runtime service account |
| Invocation identity (control plane) | Role Temporal assumes to call `InvokeFunction`/`GetFunction` | "Invoker" SA Temporal impersonates to call the Cloud Run admin API |
| Hard duration cap | 15 minutes | None in the same sense — tune `no_sync_quiet_ms` instead |
| Scale-in risk | N/A in the same way (invocation completes or times out) | Pool-level scale-in can stop an instance mid-Activity |
| Secrets | Fetched manually at cold start (e.g. from Secrets Manager via ARN) in the demo repo's pattern | Mounted natively via `--set-secrets`, no fetch code needed |
| Versioning-to-compute mapping maturity | Documented in depth (qualified ARN guidance) | Marked "content coming soon" in the Cloud Run encyclopedia page at time of writing |

---

## 10. Reference Demo: temporal-serverless-no-roads

[github.com/lainecsmith/temporal-serverless-no-roads](https://github.com/lainecsmith/temporal-serverless-no-roads)
— a live audience-participation demo (Lambda-based). Submit a name via a web
UI → triggers `DemoWorkflow` (three chained `SimulateWork` activities, ~12s
each) → dashboard shows Lambda invocations, backlog depth, workflow counts
live.

### Repo structure
```
shared/            # workflows, activities, task queue, worker config (provider-agnostic)
lambda-worker/     # deployable 1: uses lambdaworker.RunWorker, CFN execution role
demo-app/          # deployable 2: HTTP UI + API, EKS manifests
  └── localworker/ # long-polling worker for LOCAL DEV ONLY, not deployed to Lambda
```

### Key operational details from the repo
- Lambda deployed with 600s timeout, 256MB memory (matches the docs' own example exactly)
- Two-secret pattern: Secrets Manager for the API key (fetched at cold start via a secret ARN env var), fallback to a plaintext env var "for demos"
- Concurrency (`WORKER_MAX_CONCURRENT_ACTIVITIES` / `_WORKFLOWS`) is repo-specific config, not a documented Lambda-package env var — it's their own wrapper around Worker options
- Presenter mode: fires up to 200 simultaneous workflows to prime the scaling visuals
- Does **not** publish a numbered Lambda version or use a qualified ARN — uses the simpler, docs-sanctioned "fine for demos" path

---

## 11. Porting That Demo to Cloud Run

### The one-sentence insight
The repo's own README says its Lambda worker "exits after each task batch
and is not suited for local iteration — use the long-polling local worker
instead." That local-dev-only worker is **structurally what a production
Cloud Run worker already looks like.** Porting isn't "swap the cloud SDK
calls" — it's "delete the Lambda-shaped adapter, deploy the long-polling
worker as a container in a Worker Pool."

### Component mapping
| Lambda demo | Cloud Run equivalent |
|---|---|
| `lambda-worker/` (`lambdaworker.RunWorker`) | New `cloudrun-worker/` — ordinary `worker.Run()` loop + `WorkerDeploymentConfig` |
| CFN execution role (CloudWatch + Secrets Manager) | Runtime service account + `roles/secretmanager.secretAccessor` |
| CFN invocation-role template (Temporal Cloud UI) | Terraform module `serverless-workers/gcp/cloud-run` → invoker service account |
| Manual Secrets Manager fetch at cold start | `--set-secrets` — mounted automatically, no fetch code |
| Lambda function timeout (600s) | No equivalent; tune `no_sync_quiet_ms` instead |

### Demo-specific gotcha
Each `DemoWorkflow` holds a worker ~36s across three chained activities —
exactly the scenario `no_sync_quiet_ms` exists for. Left at defaults, an
instance could be scaled away mid-chain during a lull between audience
submissions.

---

## 12. Docs-vs-Demo Consistency Audit

Cross-checked the Lambda demo repo against the official AWS Lambda
deployment guide.

**Tightly in sync:** the `lambdaworker.RunWorker` usage, the 600s/256MB
example values, the two-IAM-role separation (execution vs. invocation), the
CFN template's External-ID confused-deputy protection, the
bump-Build-ID-→-new-Worker-Deployment-Version rule, and the UI's
auto-set-as-current behavior.

**Simplified, not wrong:** the repo's architecture description names
"backlog depth" as the scaling trigger, when the more precise mechanism
(per the encyclopedia docs) is sync match failure as the *primary* trigger
— though this matches how Temporal's own blog/marketing describes it too,
so it's a consistent simplification, not a repo-specific error.

**Unverifiable as written:** the repo references a "Workers → Serverless"
UI navigation item; the docs text only describes a general "Workers" page
with a "Create Worker Deployment" button, no distinct "Serverless"
sub-item mentioned.

**Real gap if reused as a template:** the repo never demonstrates the
versioned/qualified-ARN pattern the docs recommend for production — it uses
the simpler unqualified-ARN path, which the docs explicitly sanction "for
development or non-critical workloads" but flag as risky (non-determinism
errors for in-flight Pinned Workflows) beyond that.

---

## 13. DevRel / Demo Ideas

### Why a builder/village game fits this feature specifically
The genre's core mechanic — a build queue with a limited number of Builders
— **is already** a task-queue-with-scarce-workers problem, so the metaphor
is load-bearing, not decorative.

| Game concept | Maps to |
|---|---|
| A build order | One Workflow execution |
| Multi-stage construction | Chained Activities |
| The build queue | The Task Queue |
| A Builder | A Cloud Run Worker Pool instance |
| "Not enough builders, orders waiting" | Sync match failure / backlog |
| Builders arriving during a rush | WCI scaling ahead of demand (80% utilization headroom) |
| Builders not packing up instantly | Conservative scale-in + `no_sync_quiet_ms` cooldown |
| Empty village | Scale-to-zero |

### Scripted "aha moments" for a talk
1. Idle state, zero cost, say it out loud
2. Burst mode — instances appear ahead of the full backlog being visible
3. **Best original content:** show a builder scaled away mid-wall
   (`no_sync_quiet_ms` untuned) vs. the tuned version holding steady through
   a lull
4. Kill a random instance live on stage — the build resumes, doesn't restart
5. (Stretch) Ship a new Worker Deployment Version mid-demo — in-flight
   builds stay Pinned to the old version, new orders pick up Auto-Upgrade

### Naming / IP flag
Avoid a name that echoes Supercell's actual title too closely for anything
publicly branded (recorded talks, blog posts) — loop in brand/legal for
official assets. The *mechanic* (builders, a village, walls) is genre
convention, not IP. Alternative names considered: Worker's Keep, Foreman,
Outpost Builders, SiegeOps, BuildQueue.

### Adjacent concepts considered
- **Kitchen brigade** — orders in, serverless "cooks" fulfill them, ticket board fills — arguably even more universally relatable
- **Mission Control** — rocket assembly stages as chained Activities, skews toward an engineering audience
- **Before/after cost toggle** — same workload, side-by-side always-on fleet vs. scale-to-zero pool — less fun, more effective for cost-conscious/FinOps audiences

### Format fit
Same shared-dashboard build works as a self-explanatory booth demo, a
scripted keynote segment, and shareable social content (a filling-in village
is more shareable than a metrics graph) — one build, multiple devrel formats.

---

## 14. This Project: Research Fleet POC

The actual proof of concept built alongside this knowledge base, reduced to the
minimum that still exercises Serverless Workers: one Workflow, one 5-second
Activity, a Cloud Run Worker Pool Worker, and burst mode to watch the pool scale.

It was originally skinned as a build-orders game; that was stripped on
2026-07-28 so the infrastructure story stays in focus. §13 keeps the game
ideation for later.

```
research-fleet/
├── README.md             # start here — file map + local quickstart
├── CLAUDE.md             # context for Claude Code (must stay at root to load)
├── workflows.py          # HelloWorkflow (PINNED)
├── activities.py         # say_hello — one 5s Activity
├── starter.py            # starts Workflows
├── worker_local.py       # long-polling worker for local sanity checks
├── worker_cloudrun.py    # Cloud Run Worker Pool entrypoint
├── Dockerfile
├── requirements.txt
├── docs/
│   ├── TUTORIAL.md       # full step-by-step, with real gcloud commands
│   └── KNOWLEDGE_BASE.md # this file
└── terraform/            # invoker service account module
```

The layout is deliberately flat — at this size, one file per folder cost more
in navigation than it bought in structure, and it forced `sys.path` juggling
in both Workers.

See `TUTORIAL.md` for the runnable steps; the code itself embodies the
"ordinary long-running Worker, no Lambda-style adapter" principle from
§7.1 directly — that's not an accident, it's the whole point of building
this on Cloud Run rather than Lambda.

---

## 15. Glossary

| Term | Meaning |
|---|---|
| **WCI (Worker Controller Instance)** | System Workflow, one per Worker Deployment Version + compute provider, that scales Serverless Workers based on Task Queue signals |
| **Sync match (failure)** | Matching Service's attempt to hand a Task directly to an already-polling Worker; failure = no Worker was free → primary WCI trigger |
| **Task Queue backlog** | Pending Tasks with insufficient Workers; secondary WCI trigger |
| **Compute provider** | Config on a Worker Deployment Version telling Temporal how to invoke/scale the Worker |
| **Worker Pool** (Cloud Run) | A set of long-lived Cloud Run instances the WCI scales (0→N) |
| **Invoker service account** (Cloud Run) | GCP SA Temporal impersonates to scale the pool via the Cloud Run admin API — never runs the pool itself |
| **Runtime service account** (Cloud Run) | The identity the pool's actual Worker instances run as |
| **`no_sync_quiet_ms`** | How long the queue must be quiet before the WCI scales the Cloud Run pool down; set > longest Activity runtime |
| **Execution role** (Lambda) | IAM role the Lambda function itself assumes at runtime (logs, secrets access) |
| **Invocation role** (Lambda) | IAM role Temporal assumes to call `InvokeFunction`/`GetFunction` — separate from the execution role |
| **External ID** (Lambda) | A value in the invocation role's trust policy specifically to prevent confused-deputy attacks |
| **Worker Deployment Version** | Immutable identifier (deployment name + build ID) a Workflow runs against |
| **Pinned vs. Auto-Upgrade** | Versioning behavior: stay on original version until done, vs. move to the new current version at the next Workflow Task |

---

## 16. Source List

- `docs.temporal.io/evaluate/serverless-workers` — evaluation-stage overview
- `docs.temporal.io/serverless-workers` — encyclopedia (WCI mechanics, lifecycle, failure handling)
- `docs.temporal.io/production-deployment/worker-deployments/serverless-workers/aws-lambda` — official Lambda deploy guide
- `docs.temporal.io/production-deployment/worker-deployments/serverless-workers/self-hosted-setup` — self-hosted Lambda prerequisites
- `docs.temporal.io/develop/python/workers/serverless-workers/aws-lambda` — Python `lambda_worker` package reference
- `docs.temporal.io/develop/go/workers/serverless-workers/aws-lambda` — Go SDK, OTel/X-Ray specifics
- Preview branch (Cloud Run, pre-GA docs): deployment guide, Python SDK page, encyclopedia page, self-hosted setup — `.../serverless-workers/cloud-run` and `.../production-deployment/worker-deployments/serverless-workers/cloud-run`
- `temporal.io/blog/introducing-temporal-serverless-workers-deploy-temporal-workers-to-aws-lambda` — official launch blog
- `byteiota.com` — two third-party articles covering the Lambda launch (useful for seeing how the feature gets described informally, less precise than the encyclopedia docs on trigger mechanics)
- `github.com/lainecsmith/temporal-serverless-no-roads` — reference Lambda demo repo

---

## 17. Open Questions / Watch List

- Cloud Run's own "Worker Versioning" encyclopedia section was marked
  "content coming soon" at time of writing — re-check when it's filled in,
  since it may formalize a qualified-image-digest-style equivalent to
  Lambda's versioned-ARN guidance.
- Serverless Workers overall are Pre-release — expect breaking API changes;
  re-verify flag names (`no_sync_quiet_ms`, dynamic config keys) against
  current docs before any public-facing demo.
- Java and .NET SDK support, plus additional compute providers beyond
  Lambda/Cloud Run, are on the roadmap per the launch blog but not yet
  detailed anywhere fetched here.
- The "Workers → Serverless" UI nav item mentioned in the demo repo was
  never independently confirmed against docs text — check the live product
  UI directly if it matters for a talk/screenshot.
