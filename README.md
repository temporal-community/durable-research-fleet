# Research Fleet

A minimal **Temporal Serverless Workers** demo on GCP Cloud Run. One Workflow,
one Activity — a hello that takes 5 seconds. Run it locally in a minute, or
deploy the Worker to a Cloud Run Worker Pool that Temporal scales from zero.

The point isn't the Activity. It's *where the Worker runs*: start 20 Workflows
at once and watch Temporal's Worker Controller grow the pool to meet the backlog,
then shrink it back to nothing.

---

## Start here: run it locally

Needs Python 3.12+ and the [Temporal CLI](https://docs.temporal.io/cli#installation).

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Then three terminals:

```bash
# terminal 1 — the Temporal dev server (Web UI at http://localhost:8233)
temporal server start-dev
```

```bash
# terminal 2 — the Worker
.venv/bin/python worker_local.py
```

```bash
# terminal 3 — one-time setup (the Worker must already be polling), then a Workflow
temporal worker deployment set-current-version \
    --deployment-name research-fleet --build-id local --yes

.venv/bin/python starter.py Shubham --watch
```

You'll see `→ started hello-…`, then `✅ Hello, Shubham!` about 5 seconds later.
Watch the event history at http://localhost:8233.

Then the interesting one — 20 at once:

```bash
.venv/bin/python starter.py --count 20
```

Locally that just means one Worker chewing through a backlog. Deployed to Cloud
Run, it's what makes the pool scale.

> **Why that `set-current-version` step?** `HelloWorkflow` declares a versioning
> behavior (`PINNED`), which Serverless Workers require. Once a Workflow declares
> one, its Worker must run in versioned mode too — and a versioned Task Queue
> needs a *current version* to dispatch to. One command, once per dev server.
>
> **Order matters.** Run it *after* the Worker is polling. On a fresh dev server the
> Worker Deployment does not exist until something polls, so running it first fails
> with `no Worker Deployment found with name 'research-fleet'; does your Worker
> Deployment have pollers?`

### The research agent

Same Worker — it registers both apps. Add a Claude key and the web tier:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # only the research app needs this
make web-local                        # terminal 4
```

Then open **http://localhost:8000** to ask a question. The live Serverless Workers
counter is in the footer of that same page — it scales with the viewport, so the one
URL works on a phone and on a projector.

One question fans out into ~6 sub-questions researched in parallel, so locally you
watch one Worker serialise them — on Cloud Run that same burst is what takes the pool
from 0 to 6. After the draft appears the Workflow **waits for you**: the pool can go
to zero while it waits, and your `/r/<id>` link keeps working, so you can come back
later and it resumes.

> **Raise the slot count for local work.** Each research sub-question takes 1–5
> minutes (server-side web search is not fast), and the default
> `MAX_CONCURRENT_ACTIVITIES=1` makes one Worker do all six in series — 10–30 minutes
> for one question. That default exists to create Task Queue backlog for the Worker
> Controller to scale on, and **there is no Worker Controller locally**, so it costs
> you everything and buys nothing:
>
> ```bash
> MAX_CONCURRENT_ACTIVITIES=6 .venv/bin/python worker_local.py
> ```
>
> Leave the deployed default at 1 — that's what makes one question light up six
> Serverless Workers. Also consider `RESEARCH_EFFORT=low` while iterating.

---

## The files

All the code is flat at the root — no packages, no import paths to reason about.

```
runtime.py    workflows.py  activities.py  starter.py
worker_local.py  worker_cloudrun.py
Dockerfile    requirements.txt
docs/  terraform/  selfhosted/
```

| File | What it is |
|---|---|
| `runtime.py` | **The infra half.** Connects, builds a versioned Worker, handles SIGTERM. App-agnostic — pass different Workflows/Activities and nothing here changes. |
| `workflows.py` | `HelloWorkflow` — calls the Activity. Declares `PINNED`, which Serverless Workers require. |
| `activities.py` | `say_hello` — sleeps 5s and returns a greeting. |
| `starter.py` | Starts Workflows. `--watch` to follow, `--count N` to burst. |
| `worker_local.py` | The Worker, for local dev. Registers both apps. Run this while learning. |
| `worker_cloudrun.py` | The same Worker, for the Cloud Run Worker Pool. |
| `Dockerfile` | **One image, two entrypoints.** Default is the Worker; the Cloud Run Service overrides the command to run `uvicorn`. |
| `terraform/` | The whole GCP stack. See `terraform/README.md`. |
| `selfhosted/` | Docker Compose Temporal for local use. |

The research agent — the real application. `HelloWorkflow` above stays as the
infrastructure smoke test, because it needs no Claude key.

| File | What it is |
|---|---|
| `llm.py` | The Claude seam. Server-side web search, `pause_turn` resume, refusal fallbacks, token accounting. Imports no `temporalio`. |
| `research_activities.py` | `plan_research`, `research_subquestion` (the fan-out unit), `synthesize`. Heartbeats on a timer and checkpoints for resume. |
| `research_workflow.py` | `ResearchWorkflow` — fan out, draft, **wait for a human**, optionally dig deeper. `progress` Query drives both UIs. |
| `research_types.py` | Transport dataclasses shared by the Workflow and its Activities. |
| `web.py` + `web/` | The single-page research **workbench** — ask, morph into a sectioned report, outline rail left, cited sources rail right, live fleet counters in the footer. A `Learn` button opens the 14 cards from `learn/README.md` in a dialog. FastAPI, vanilla JS, self-hosted fonts, no build step, no database. Runs on a Cloud Run **Service** — not a Worker Pool. |

> **Why the Activity sleeps.** Serverless scaling is driven by Task Queue
> backlog. If the Activity returned instantly, one Worker would absorb any burst
> and the pool would never need to grow. The 5 seconds is what makes scaling
> observable — tune it via `GREETING_SECONDS` in `activities.py`.
>
> For the same reason each Worker takes **one Activity at a time**
> (`MAX_CONCURRENT_ACTIVITIES=1`). The SDK default of 100 would let a single
> instance absorb a 20-Workflow burst in 5s, leaving no backlog for the Worker
> Controller to react to. With the limit, `--count 5` takes ~25s locally — that
> queue is the signal the pool scales on.

## Running a different app on this infrastructure

`runtime.py` is app-agnostic. To swap the app layer in — a research agent, say —
replace `workflows.py` / `activities.py` and bind them:

```python
from runtime import run_worker
asyncio.run(run_worker([MyWorkflow], [my_activity]))
```

Everything else (connection, TLS selection, Worker Versioning, slot limits,
graceful shutdown) comes from `runtime.py` and the environment. Terraform passes
`TEMPORAL_DEPLOYMENT_NAME`, so renaming the deployment is a variable change, not
a code change.

If you're reading the code for the first time, go
`activities.py` → `workflows.py` → `starter.py` → `runtime.py`. The first three
are the app and are tiny; `runtime.py` is the infra and is the only file with any
real machinery in it.

## Deploy to GCP

The whole cloud stack is Terraform. From this directory:

```bash
make up      # build CLI + image, then apply everything
make status  # pool instance count + VM state
make down    # destroy it all
```

That creates a VPC, a self-hosted Temporal Service on a GCE VM with the Worker
Controller enabled, and a Cloud Run Worker Pool that reaches Temporal privately
over Direct VPC egress — no tunnel, nothing publicly exposed. See
`terraform/README.md` for what it builds and the four gotchas it works around.

`make help` lists everything.

## The docs

| Doc | Read it when |
|---|---|
| **`learn/README.md`** | You're new to Temporal or Serverless Workers. 14 short cards, zero to "I get it". Start here. |
| **`terraform/README.md`** | You're deploying. What the stack creates and why. |
| **`docs/TUTORIAL.md`** | You want the manual step-by-step behind what Terraform automates. |
| **`docs/KNOWLEDGE_BASE.md`** | You want to understand *why* — how the autoscaling works, Cloud Run vs Lambda, gotchas. It's long; skim the table of contents. |
| `CLAUDE.md` | Context for Claude Code. Not meant for humans, but harmless to read. |

---

## Configuration

Both workers and the CLI read the same environment variables. Locally you can
skip all of them — the defaults point at `temporal server start-dev`.

| Variable | Default | Notes |
|---|---|---|
| `TEMPORAL_ADDRESS` | `localhost:7233` | `<namespace>.<account>.tmprl.cloud:7233` for Cloud. |
| `TEMPORAL_NAMESPACE` | `default` | |
| `TEMPORAL_TASK_QUEUE` | `research-queue` | |
| `TEMPORAL_API_KEY` | *(unset)* | Required for Temporal Cloud. Setting it turns TLS on. |
| `TEMPORAL_TLS` | auto | Override the above. `true`/`false`. |
| `TEMPORAL_DEPLOYMENT_NAME` | `research-fleet` | Must match the Worker Deployment the compute config is attached to. Terraform passes this in; a mismatch makes Workflows hang with no error. |
| `BUILD_ID` | `local` / `v1` | Must match the Worker Deployment Version's build id. |
| `MAX_CONCURRENT_ACTIVITIES` | `1` | Activity slots per Worker. See the note above — raising it can hide the scaling behaviour. |
| `GRACEFUL_SHUTDOWN_SECONDS` | `20` | Drain window for in-flight Activities after SIGTERM. |
| `ANTHROPIC_API_KEY` | *(unset)* | Research app only. The hello app and `make verify SCALE=1` work without it. In production it comes from Secret Manager, never a plaintext env var. |
| `CLAUDE_FALLBACKS` | `true` | Server-side refusal fallback, so a spicy audience question can't end the demo. `false` is the stage kill switch. |
| `CLAUDE_TIMEOUT_SECONDS` | `300` | Per-**round** HTTP timeout. Web search is slow; 120s was measured to be far too tight. The budget that must close is `rounds × this < start_to_close` (3 × 300 < 1200). |
| `RESEARCH_EFFORT` | `medium` | The biggest lever on how long the room waits. `high` researches better and is noticeably slower. |
| `PLAN_EFFORT` | `medium` | Planning is schema-constrained; more effort is waste. |
| `SYNTHESIS_EFFORT` | `high` | Cheap relative to research — one call over collected findings. |
| `DEMO_PASSCODE` | *(unset)* | Web tier only. Shared code for the public phone page; empty means open. |

---

## Deployment status

The local path above works today. Deploying the fleet to Cloud Run needs one
more thing: the **GCP Cloud Run compute provider is implemented but not yet in a
released Temporal CLI** — the `--gcp-cloud-run-*` flags exist only on the `main`
branch of [`temporalio/cli`](https://github.com/temporalio/cli), and the docs
page for it isn't published yet. Temporal Cloud additionally needs Pre-release
access granted on your Namespace; a self-hosted server does not.

`docs/TUTORIAL.md` has the details and the workaround.

## License

Apache-2.0 — see [LICENSE](LICENSE).

Copyright 2026 Temporal Technologies, Inc.

Bundled fonts are redistributed under the SIL Open Font License; see
`web/fonts/LICENSE-Archivo.txt` and `web/fonts/LICENSE-IBMPlexMono.txt`.
The Temporal wordmark in `web/temporal-logo.svg` is a trademark of Temporal
Technologies and is included for use in this demo, not licensed under Apache-2.0.
