# Research Fleet — Instructions for Coding Agents

> **Gemini fork, 2026-08-12.** This copy replaces the original provider-specific
> seam with the Google Gen AI SDK, Gemini 3.6 Flash, and Google Search grounding.
> The upstream repository is reference-only and must not be used as a push target.
> Provider-specific changes belong in this repository.

> **Renamed 2026-07-29** from folder `builder-bot-poc`, prefix `builder-bot`, queue
> `builder-queue`, image `builder-worker`. Anything still saying those is stale —
> EXCEPT `docs/KNOWLEDGE_BASE.md` §13, where "Builder" is the *game metaphor*
> (a Builder = a Cloud Run instance) and is correct as written. The rename recreated
> the entire GCP stack from scratch; `builder-bot-vpc`/`-subnet` may linger as
> Terraform-orphaned resources until Cloud Run releases their `serverless-ipv4-*`
> reservations, after which delete them by hand.

> **Start here if you have no context.** Everything you must not break is in THIS
> file — "Deployment gates", "Known constraints", and the bug lists below. They are
> the durable record and they are committed.
>
> References to `decisions/` throughout are a **local-only, gitignored** folder of
> longer-form rationale (numbered D-x.y notes). It is **not in the repo**, so on a
> fresh clone those pointers will not resolve — that is expected, not a broken link.
> Nothing in this file depends on it: where a decision matters, the reason is stated
> here too.

## What this is
A Temporal **Serverless Worker on GCP Cloud Run**, running **two apps on one Task
Queue**:

1. **`HelloWorkflow`** → `say_hello` (5s sleep). The **infrastructure smoke test**.
   Needs no Gemini key, which is what keeps `make verify SCALE=1` usable for
   diagnosing scaling problems without spending tokens. Keep it.
2. **`ResearchWorkflow`** → the real app (added 2026-07-29). A conference demo: the
   audience asks research questions from their phones, each question fans out into
   parallel sub-questions researched with Gemini + server-side web search, and a
   projector shows the live Serverless Workers count.

The Worker runs in a Cloud Run Worker Pool that Temporal's Worker Controller
Instance scales 0→N; `starter.py --count N` bursts the hello app, and one research
question fans out to ~6 sub-questions which does the same thing for real.

**`runtime.py` did not change to add the second app.** That was the point of the
seam (`decisions/03-code-and-tests.md` D-3.1) and it is now proven rather than
claimed. Don't put app logic in it.

The demo was previously skinned as a "build orders" game (lay_foundation →
raise_walls → finish_build); that was stripped on 2026-07-28. **Don't re-add game
flavour unless asked.**

**Read `docs/KNOWLEDGE_BASE.md` first** — it's the full research base (docs,
official blog, a reference demo repo, a docs-vs-demo consistency audit, and
devrel ideation) this project was built from. `docs/TUTORIAL.md` is the runnable
step-by-step with real `gcloud` commands (GCP project: `serverless-workers-demo`).
`README.md` is the human front door: file map plus the local quickstart.

## Layout
Deliberately flat — one file per folder wasn't worth the navigation cost at this
size, and it forced `sys.path` juggling in both Workers. Don't re-introduce
`shared/`, `cli/`, `worker/`, `localdev/`.

- ✅ `runtime.py` — **the infra half**: connect, versioned Worker, slot limits,
  SIGTERM graceful shutdown. App-agnostic; `run_worker(workflows, activities)` is
  the seam the Research agent app plugs into. Don't put app logic here.
- ✅ `workflows.py` — `HelloWorkflow`, `versioning_behavior=PINNED`, explicit
  `heartbeat_timeout` + `RetryPolicy`
- ✅ `activities.py` — `say_hello`, one 5s sleep. The sleep is load-bearing: without
  it there's no backlog, so nothing for the WCI to scale on. `GREETING_SECONDS`.
- ✅ `starter.py` — starts hello Workflows; `--watch`, `--count N` for burst mode
- ✅ `worker_local.py` / `worker_cloudrun.py` — register **both** apps; ~12 lines each
- ✅ `Dockerfile` — **one image, two entrypoints.** `CMD` is the Worker; the Cloud Run
  *Service* overrides `command` to run `uvicorn`. Dropping that override makes the web
  tier start a second Worker that absorbs the burst and silently kills the demo.
- ✅ `terraform/` — the whole GCP stack, one `make up` / `make down`
- ✅ `selfhosted/` — Docker Compose Temporal, local dev only (see gate #2 below)
- ✅ Deployed and verified on Cloud Run, including WCI scale-from-zero (see below)

The research app (rationale summarised below; longer notes live in the local-only `decisions/`):

- ✅ `llm.py` — **the Gemini seam.** Imports no `temporalio` and must not: it stays
  unit-testable without a server, and the retry story lives in exactly one place.
  `HttpRetryOptions(attempts=1)` on purpose — Temporal is the only retry layer.
- ✅ `research_activities.py` — `plan_research`, `research_subquestion` (the fan-out
  unit), `synthesize`. Heartbeats on a **timer** during long Gemini requests.
- ✅ `research_workflow.py` — `ResearchWorkflow`, PINNED, fan-out → draft → **human
  review pause** → optional second fan-out. `progress` Query feeds both UIs.
- ✅ `research_types.py` — transport dataclasses, no third-party imports
- ✅ `web.py` + `web/index.html` + `web/app.css` + `web/fonts/` — **one** page: a
  research **workbench**. Ask → morph → sectioned report, with an outline rail (left),
  a sources rail carrying the citation targets (right), and the live fleet counters in a
  sticky footer that scales with the viewport, so the same URL serves a phone and a
  projector. FastAPI, vanilla JS, no build step, no database. Runs on a Cloud Run
  **Service**, not a Worker Pool. Must not import `llm`/`google.genai`.
  - **The rails exist to shorten the scroll, not to decorate.** Sources and the parts
    ledger live outside the reading column; that is what uses a 1600px screen *and*
    stops the report being a mile long. Below 900px they become a tablist.
  - **The centre grid track is capped in `ch`, not `1fr`.** As `1fr` it grew to fill the
    viewport while the prose stayed at its reading measure, leaving a dead gap between
    report and sources. Cap the track and centre the grid.
  - **ZERO `innerHTML` in `web/index.html`.** The report is model output rendered as
    markdown, so this is the XSS boundary: every node is `createElement`, every text run
    `textContent`, clearing is `replaceChildren()`. Guarded by
    `test_the_page_never_uses_innerhtml`. Verified in a real browser: a `javascript:`
    link is dropped and a `<script>` inside a code fence stays text.
  - **Citations are bounded.** `[n]` becomes a link only when `1 ≤ n ≤ len(sources)`;
    out-of-range stays plain text, because models miscount and a footnote pointing at
    nothing is worse than no footnote.
  - **`/dashboard` was retired 2026-07-29.** Don't re-add a second page; the footer is
    the dashboard. `dashboard_url` is gone from Terraform outputs too.
  - **Fonts and the logo must be SERVED, not just shipped.** `COPY web/ ./web/` puts
    them in the image, but `web.py` hands out files by explicit route — hence the
    `/fonts` `StaticFiles` mount and the `/temporal-logo.svg` route. Without them the
    page silently falls back to system type.
  - **`/api/fleet` is cached for 1s** (`FLEET_TTL_SECONDS`). Every phone now polls it,
    and each uncached read costs 3 Temporal RPCs. The `peak`/`started` rising-edge
    derivation lives in `_read_fleet()` — inside the refresh — on purpose; running it
    per request against a cached value makes `started` drift.
  - Brand tokens in `app.css` are now the **real** temporal.io/brand values (UV
    `#444CE7`, Space Black `#141414`, Off White `#F8FAFC`). Every other colour is
    derived with `color-mix()`, so no brand colour is invented. **UV on Space Black is
    only ~3.1:1** — large text and UI only, never body copy.

Infra identifiers are `research-fleet` (prefix, deployment name) and
`research-queue` (task queue), renamed from `builder-bot`/`builder-queue` on
2026-07-29. **Renaming the prefix again means destroying and recreating the whole
stack** — the names are baked into every resource — so don't do it casually; the
2026-07-29 pass cost a full teardown, a fresh VM bootstrap and a new demo URL.

## Verification status

The Temporal/Cloud Run infrastructure observations below come from the upstream
project's 2026-07 deployments. They remain useful evidence for the platform wiring,
but they are not proof that this Gemini fork has been deployed.

For the Gemini fork:

- All 104 Python tests and all 10 Terraform tests pass locally. The provider seam
  uses offline fakes; tests must never make a real Gemini request.
- `llm.client()` is lazy. The hello app and `make verify SCALE=1` work without
  `GEMINI_API_KEY`; the first research call fails with a clear `RuntimeError`.
- Google Search grounding is one atomic GenerateContent request. Timer heartbeats
  preserve liveness, but there is no partial model output to checkpoint. If scale-in
  kills that request, Temporal retries it from the beginning. Already completed
  sibling findings and the plan remain durable and are not rerun.
- A real Gemini API research run and a fresh GCP deployment are still required before
  adding latency, token, search-count, or cache-hit claims to this file.

Local infrastructure smoke test after any change: one Workflow ≈5.3s and
`starter.py --count 5 --watch` ≈25s. The serialization is deliberate:
`MAX_CONCURRENT_ACTIVITIES=1` creates the backlog that the WCI scales on. If five
Workflows finish in roughly 5s, the slot limit has been lost.

## Cost was removed from the app on 2026-07-29
Both the UI display and the arithmetic. There is **no dollar figure anywhere** — no
pricing table in `llm.py`, no `usd` key in the `progress` Query, no cost tile on the
dashboard. Two attempts at one both produced a wrong number on a projector, and a
hardcoded rate card in a repo that gets demoed months later goes stale silently.
Tokens, cache reads and searches are the units; they measure *work not redone*, which
is Temporal's actual claim. Guarded by `test_usage_carries_no_money` and
`test_no_money_anywhere_in_the_query`. **Don't add a cost counter back.**

Bugs found and fixed on 2026-07-28 — the project had never been run before then.
Don't reintroduce:
1. Neither `starter.py` nor `worker_cloudrun.py` may hardcode `tls=True`. The
   self-hosted frontend is plaintext gRPC. TLS is derived from
   `TEMPORAL_API_KEY` presence, overridable via `TEMPORAL_TLS`. The pool sets
   `TEMPORAL_TLS=false` and it is load-bearing.
2. `worker_local.py` **needs** `WorkerDeploymentConfig` with
   `use_worker_versioning=True`. Because `HelloWorkflow` declares a versioning
   behavior, an unversioned Worker gets every Workflow Task rejected
   ("versioning behavior cannot be specified without deployment options being
   set with versioned mode") and retries forever. Local dev also needs a one-time
   `temporal worker deployment set-current-version --deployment-name research-fleet
   --build-id local`.
3. Terraform needs google provider **`~> 7.0`** (Worker Pools don't exist in 4.x).
   That's why the stack does *not* use the upstream
   `terraform-modules//serverless-workers/gcp/cloud-run` module, which pins
   `~> 4.0` — the two are unsolvable together. Its 3 resources are inlined in
   `terraform/iam.tf`.
4. Images must be built `--platform linux/amd64`. An arm64 Mac build fails on
   Cloud Run with an exec-format error that doesn't point back at the cause.

A high-effort review on 2026-07-28 found 10 more, all fixed. The ones most worth
not regressing:
5. **`MAX_CONCURRENT_ACTIVITIES` must stay at 1.** The SDK default is 100, which
   lets one instance absorb a whole burst so no Task Queue backlog forms and the
   WCI never scales — the demo silently does nothing. Verified: `--count 5` takes
   ~25s with the limit vs ~5s without. Also KB §4 slot isolation.
6. **`deployment_name` must not be hardcoded in the workers.** It comes from
   `TEMPORAL_DEPLOYMENT_NAME` (Terraform sets it). Hardcoding it means Terraform
   can register the compute config against one deployment while the pool
   registers under another — Workflows then hang with no error at all.
7. **The bootstrap script must fail loudly.** It uses `set -Eeuo pipefail` + an
   ERR trap writing `/var/log/bb-failed`, and only touches `/var/log/bb-ready` on
   success. It previously reported success after every step failed.
8. **`terraform/imports.tf` is opt-in** (`adopt_existing`, default false). Unconditional
   import blocks made `apply` impossible on any clean project.
9. **`.gitignore` needs `*.tfstate*`**, not exact names — Terraform writes
   timestamped backups that an exact-name rule misses, and state contains SA
   emails and project ids.
10. **`Dockerfile` must COPY `runtime.py`** or the image crashes on start.

Found on the **first real end-to-end deploy, 2026-07-29** (everything above was
found locally or on a partial stack):

11. **The Gemini secret must hold a version BEFORE the pool is created.** The
    pool mounts `GEMINI_API_KEY` as a `secret_key_ref` pinned to `latest`, and
    Cloud Run resolves that at *create* time: an empty secret fails the pool with
    `Error code 9 ... versions/latest was not found`. Terraform creates the secret
    empty on purpose (the key must never enter state — D-5.11) and created the pool
    in the same apply, so **`make up` could never have worked from scratch.** Fixed
    with a `make secret` target (same `-target` idiom as `repo`) sequenced before
    `apply`; it seeds the real key, or a placeholder when `GEMINI_API_KEY` is
    unset, and never clobbers an existing version. The old comment in
    `workerpool.tf` asserting an empty secret was tolerable has been corrected.
12. **The web tier is NOT public. Don't make it public.** ⚠️ This gate said the
    opposite until 2026-07-31 — it recommended `invoker_iam_disabled = true` — and
    that recommendation was wrong. The history matters because the dead ends are real:
    - Temporal's org enforces Domain Restricted Sharing, so an `allUsers` +
      `roles/run.invoker` binding is refused outright (*"One or more users named in
      the policy do not belong to a permitted customer"*). The original
      `google_cloud_run_v2_service_iam_member.web_public` could not be created and the
      phone page 403'd the room.
    - `invoker_iam_disabled = true` (provider ≥ 7.x; `--no-invoker-iam-check`) is
      Google's documented remedy for that constraint, and it **worked** — anonymous
      `GET /` → 200. It also produces **exactly the exposure the org policy exists to
      prevent**, and the Cloud Run console reports the Service as "Authentication:
      Public Access" regardless of `ingress`. An external scan flagged it.
    - **Current posture:** `ingress = INGRESS_TRAFFIC_INTERNAL_ONLY`,
      `invoker_iam_disabled = false`, and `roles/run.invoker` granted only to named
      in-domain users via `var.web_invoker_users` (empty by default). Asserted by
      `terraform test` → `the_web_tier_is_a_service_not_a_worker_pool`.
    - **The demo does not need a public page.** The presenter runs `make web-local`
      against this project's Temporal over the SSH tunnel, so the fan-out and
      scale-from-zero are still real Cloud Run behaviour with no public surface.
    - "Nobody can guess the hostname" is not a control: Cloud Run's newer URL form is
      `<service>-<project-number>.<region>.run.app`, and both the service name and the
      region are in this public repo.
    - `demo_passcode` stays set regardless. It lives in `terraform/terraform.tfvars`
      (gitignored, auto-loaded) rather than a `-var`, because a plain `make apply`
      would otherwise silently reset it to `""`.
    - Other dead ends: an external HTTPS Load Balancer still requires `allUsers`
      invoker on the Service; the DRS-exception route needs policy-admin at the
      org/folder plus custom org policies with resource tags (a project owner
      **cannot** override an inherited org policy); and `gcloud run services proxy`
      needs a `cloud-run-proxy` binary that Homebrew gcloud's `components install`
      will not fetch (it reports "All components are up to date" while the binary
      stays missing).
    - **12b — the VM's public IP had the Temporal Web UI open on `:8233`.** Same
      2026-07-31 scan. Unauthenticated, it lists namespaces, exposes every Workflow's
      event history (i.e. the research questions and answers) and permits
      terminate/cancel/signal. Nothing in this configuration opened 8233 — an allow
      rule was created outside Terraform, and GCP's implied deny cannot help once an
      explicit allow exists. **Fixed structurally:** `deny_public_to_vm` in
      `network.tf` denies all ingress from `0.0.0.0/0` at **priority 50**, while the
      two intended allows (subnet→7233, operator→22) sit at **priority 10**. Anything
      created later at the default priority of 1000 is inert. Don't renumber these
      without reading `temporal_frontend_is_never_public`.

13. **`.dockerignore` excludes `learn/`, so `COPY learn/README.md` needs a negation.**
    `!learn/README.md` is in `.dockerignore` and is load-bearing — an exclusion removes
    the path from the build *context*, so no `COPY` can reach it and the whole build
    fails with `"/learn/README.md": not found`. Verify with
    `docker run --rm <IMAGE> head -1 learn/README.md`, not by reading the Dockerfile.
14. **Pushing the same image tag does NOT redeploy the Worker Pool.** Cloud Run pins
    the digest when a revision is created, and `terraform apply` sees no config change
    for the pool, so new worker code silently never arrives — only the web Service
    updates, because its config does change. After `make image`, force a pool revision:
    ```
    DIGEST=$(gcloud artifacts docker images describe <repo>:v1 --format='value(image_summary.digest)')
    gcloud run worker-pools update research-fleet-worker-pool --image <repo>@$DIGEST --region ...
    ```
    That leaves harmless Terraform drift (digest vs tag) which the next apply resolves
    to the same image. Check with `worker-pools describe` that the image is a digest.
15. **`uvicorn --no-proxy-headers` is required.** Its proxy middleware is ON by default
    and rewrites `request.client` from the FIRST `X-Forwarded-For` entry — the one value
    a caller can forge — which silently defeats the per-IP ask limit.

16. **Every Worker needs a UNIQUE Temporal identity, or the headline number reads 1.**
    The SDK default is `{pid}@{hostname}`; in a Cloud Run container the worker is PID 1
    and the hostname is `localhost`, so EVERY instance reports `1@localhost`. The
    Serverless Workers counter counts distinct poller identities, so it was pinned at
    **1 while six instances researched in parallel** — the demo's central number, wrong
    on stage and right locally (real pids/hostnames), which is why no test caught it.
    Fixed in `runtime.worker_identity()`, wired into `Client.connect(identity=...)`,
    guarded by `test_worker_identity_is_unique_per_process`.
    - Proof it was real, from a live run's history: six Activities all started in the
      same second and overlapped (impossible on one instance with 1 slot), yet all
      reported `1@localhost`.
    - After the fix, a live fan-out read `fleet=10..20` against `CloudRun=13..20`.
    - **The two numbers will never match exactly, and that is correct.** Cloud Run
      counts PROVISIONED containers (including ones still booting); `/api/fleet` counts
      workers actually POLLING, and poller entries age out over ~5 minutes so it
      briefly overcounts during scale-in. They bracket the truth from opposite sides.

## Deployment gates (as of 2026-07-28 — verify before assuming)
1. **The CLI flags aren't released.** `--gcp-cloud-run-*` exist only on
   `temporalio/cli` **main**; v1.7.1 and v1.8.1 have `--aws-lambda-*` only.
   `make cli` builds a linux/amd64 binary from main; Terraform ships it to the VM
   via GCS since there's no public release asset.
2. **The SERVER must also be unreleased.** Released `temporalio/server:1.31.2`
   contains the provider strings and accepts every `workercontroller.*` key —
   then fails at `create-version` with *"Could not instantiate scaling algorithm
   with type 'rate-based'"*. Only the server bundled with CLI main
   (1.32.0-158.0) succeeds. Don't trust binary strings or config acceptance as
   evidence the feature works; run `create-version`.
3. **`rate-based` must be in `scaling_algorithms.enabled`, and the docs don't
   say so.** The published self-hosted page lists only `no-sync` — that's the
   Lambda config. Cloud Run needs both.
4. **The Pre-release gate is Temporal Cloud-side only** — self-hosted needs no
   grant. Dynamic config uses repeated `--dynamic-config-value`;
   `--dynamic-config-file` does not exist.
5. `worker deployment create` pre-defines a deployment so `create-version`
   works with **no poller** — that's the serverless path. Don't start a worker
   first to "register" it.
6. `iamcredentials.googleapis.com` must be enabled or you get
   `iam.serviceAccounts.getAccessToken` denied, which looks like missing IAM.
7. Cloud Run instances must reach the Temporal frontend. Solved by putting the
   VM and the pool in one subnet with Direct VPC egress — no tunnel, nothing
   public. Cloudflare Tunnel **cannot** do this (no gRPC over public hostnames,
   any plan); ngrok TCP requires a card on file.

**SOLVED 2026-07-28: WCI scale-from-zero works.** Pool 0 → 3 in 20s for 5 queued
Workflows, then back to 0. Two things were required:
- **`iam.serviceAccounts.actAs` on the pool's RUNTIME service account, granted to
  the invoker** (`terraform/iam.tf` → `invoker_acts_as_worker_rt`). Neither KB
  §7.6 nor the upstream Terraform module grants this. Without it
  `UpdateWorkerSetSize` fails every attempt and the pool never scales, with **no
  error anywhere in GCP** — only in the Temporal server's own debug log.
- The Version must have Task Queues attached, which only happens once a Worker
  polls. Run the pool at 1 instance once after a fresh apply, then drop to 0.

**FULL STACK DEPLOYED AND VERIFIED 2026-07-29.** `make verify SCALE=1` against the
live stack: **16 passed, 0 failed**, including §8 scale-from-zero (pool forced to 0,
5 Workflows queued, `t+20s instances=2`). Also confirmed on the real deploy:
`create-version` succeeded with `ComputeConfigSummary gcp-cloud-run` — the step gate
#2 says fails on a released server — and `no-sync AND rate-based` both enabled. The
only resource that would not apply is the public invoker binding (gate #12).

The upstream research app also demonstrated pool scale-out for a six-way fan-out and
the full plan → fan-out → draft → review → Signal lifecycle. Do not reuse its API
latency, token, search, or cache measurements as Gemini baselines; remeasure them.

**`egress = "PRIVATE_RANGES_ONLY"` on the pool is load-bearing for the research
app.** Private traffic reaches the Temporal VM at `10.10.0.10:7233` through the VPC,
while `generativelanguage.googleapis.com` goes out over Cloud Run's default internet
egress. There is deliberately **no Cloud NAT** and none is needed. Switching this to `ALL_TRAFFIC`
would route Gemini calls into a subnet with no NAT and break every research Activity
while `make verify SCALE=1` — which needs no Gemini key — kept passing. That failure
would look like a broken app, not a network change.

## Known constraints (don't "fix" these — they're intentional)
- **`worker_cloudrun.py` is deliberately an ordinary long-polling Worker**, not
  a per-invocation adapter like AWS Lambda's `lambdaworker` package. This is
  correct for Cloud Run — see docs/KNOWLEDGE_BASE.md §7.1. Don't "port" a
  Lambda-style exit-after-batch pattern in here.
- **Every Workflow needs a versioning behavior** (`PINNED` or
  `AUTO_UPGRADE`) — this is a hard Serverless Workers requirement, not
  optional boilerplate.
- **The hello app is not a placeholder — it's the infrastructure smoke test.** It
  needs no Gemini key, so `make verify SCALE=1` can diagnose a scaling problem
  without spending tokens. Don't delete it now the research app exists.
- The 5s sleep is not arbitrary — it creates the backlog the WCI scales on.
  Don't "optimise" it away.

Research app (full reasoning in `decisions/05-research-agent.md`):
- **`MAX_CONCURRENT_ACTIVITIES` stays 1.** With one slot per instance, one question
  → ~6 sub-questions → 6 Serverless Workers. That mapping *is* the demo.
- **Heartbeat on a timer during the call.** A research Activity is one long `await`;
  without timer heartbeats a healthy call can exceed `heartbeat_timeout` and be
  killed.
- **`llm.py` must not import `temporalio`; `web.py` must not import `llm`/`google`.**
  Both enforced by `tests/test_serverless_contract.py`.
- **Temporal is the only retry layer** (`HttpRetryOptions(attempts=1)` on the Google
  Gen AI client). So
  `RESEARCH_RETRY` has to ride out a 429 alone — hence the 60s max interval.
- **The timeout budget closes.** The 300s Gemini HTTP timeout stays below the 1200s
  Activity `start_to_close`; `heartbeat_timeout` stays 60s because timer heartbeats
  decouple liveness detection from call duration.
- **Every Activity that calls Gemini must return its `Usage`.** `plan_research`
  originally returned a bare list and its tokens vanished from the cost counter.
- **Thinking level is an env var** (`RESEARCH_EFFORT`, default `medium`; supported:
  `minimal`, `low`, `medium`, `high`) because it is the demo's latency knob.
- **Keep `SYSTEM_RESEARCH` constant.** Gemini implicit caching benefits from common
  prefixes. Do not interpolate request-specific text into the system instruction.
- **Google Search is a built-in server tool.** Do not add a local scraper or a
  client-side tool loop unless the architecture is deliberately being changed.
- **No time-skipping is possible in tests.** The test server rejects Worker
  Versioning, which every Workflow here requires. Not a preference — a hard block.
- **Don't deploy a new `BUILD_ID` while a Workflow is awaiting review.** Parked
  executions are PINNED to the Version they started on.

## Natural next steps (pick based on what's being asked for)
- **If asked to prove the scaling loop**: see the open gap above — register task
  queues by running the pool once, then scale to 0 and watch `make status`.
- **If asked to "make it a real game"**: docs/KNOWLEDGE_BASE.md §13 has the
  "Clash of Workers" concept. Skin *around* the Workflow/Activity core; don't
  entangle game state with it.
- **If asked to actually deploy it**: `make up` (Terraform owns everything).
  `docs/TUTORIAL.md` is the manual walkthrough behind it and assumes project
  `serverless-workers-demo` / region `us-central1` — confirm before anything
  destructive.
- **If asked to add resilience/observability**: see docs/KNOWLEDGE_BASE.md §7.7
  for the OpenTelemetry Cloud Run pattern, and §7.3 for why
  `no_sync_quiet_ms` + Activity Heartbeats matter once Activities get longer
  than the current 5s placeholders.
- **If asked to port to Lambda for comparison**: see docs/KNOWLEDGE_BASE.md §11
  for the exact component-by-component mapping already worked out.
