# Durable Research Fleet

A deep-research agent that runs on **Temporal Serverless Workers** on Google Cloud Run.

Ask a question from your phone. It splits into ~6 independent sub-questions, each
researched in parallel by a Cloud Run instance that **did not exist a second ago**,
then merged into one cited report. The pool scales `0 → N → 0` on its own.

![The research console](docs/images/hero-console.png)

---

## What you're looking at

Outline on the left, the cited report in the middle, sources on the right, live fleet
counters along the bottom. When the draft is ready the Workflow parks and waits for
you — that's `WAITING FOR YOU`, with the review card in the left rail.

![Live fleet counters](docs/images/footer-workers.png)

Two beats make the demo:

- **Fan-out** — six sub-question Activities are scheduled at once. One Activity slot
  per instance means most can't be picked up by the current pool, so the Worker
  Controller scales out to meet them.
- **Wake from zero** — after the draft the Workflow holds no task at all, so the pool
  drops to **zero with a live Workflow still in flight**. Your approval wakes it.

Temporal's timeline shows both: six `research_subquestion` Activities overlapping,
then `synthesize`, then a 2-hour timer ended early by the `review` Signal.

![Temporal timeline: six concurrent activities, then the review pause](docs/images/temporal-timeline.png)

And Cloud Run's own metrics, from the other side — instance count climbing and
returning to zero, over and over:

![Cloud Run worker pool scaling from zero](docs/images/cloud-run-pool.png)

---

## Run it locally

No GCP and no Claude key needed for the infrastructure path.

```bash
git clone org-195963520@github.com:temporal-community/durable-research-fleet.git
cd durable-research-fleet
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
# 1 — Temporal dev server (Web UI on http://localhost:8233)
temporal server start-dev

# 2 — the Worker
.venv/bin/python worker_local.py

# 3 — one-time, AFTER the Worker is polling
temporal worker deployment set-current-version \
  --deployment-name research-fleet --build-id local --yes

# 4 — a Workflow, then a burst
.venv/bin/python starter.py --watch
.venv/bin/python starter.py --count 5 --watch
```

Step 3 is not optional: every Workflow here declares a versioning behavior, and an
unversioned Worker gets **every** Workflow Task rejected.

### The research agent

Needs `ANTHROPIC_API_KEY` — put it in `.env`, which is gitignored.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
make web-local        # http://localhost:8000
```

One question ≈ 6 sub-questions, ~90 web searches, 3–6 minutes, ~500K tokens.

```bash
make test             # 111 offline tests — no server, no API key
```

---

## Deploy to GCP

```bash
make up AUTO=1            # CLI, Artifact Registry, image, secret, Terraform
make register-queues      # once, so the Version learns its Task Queues
make verify SCALE=1       # 16 checks, including a real scale-from-zero
```

`terraform output demo_url` is the page you hand out. `AUTO=1` skips the plan
prompt — without it `make up` cannot complete unattended.

> **Serverless Workers is Pre-release.** The Cloud Run compute provider is not in a
> released Temporal CLI or server — `make cli` builds one from `temporalio/cli@main`
> and Terraform ships it to the VM. Temporal Cloud additionally needs Pre-release
> access on your Namespace; self-hosted does not. See `docs/TUTORIAL.md`.

One trap worth knowing: **after a rebuild, force the pool onto the new digest.**
Pushing the same image tag does not redeploy it — Cloud Run pins the digest when a
revision is created, so new worker code silently never arrives. `CLAUDE.md` gate #14
has the command.

---

## How it works

```
phone ──► Cloud Run Service (web.py) ──► Temporal ──► Cloud Run Worker Pool
          request-serving, public          on a VM      long-polling Workers,
          NOT a Serverless Worker                      scaled 0→N by the WCI
```

One image, two entrypoints: the pool runs the Worker, the Service overrides the
command to run `uvicorn`. There is no database — each run's state lives in Temporal's
event history, which is the durability argument made structurally rather than claimed.

Two apps share one Task Queue. `HelloWorkflow` is the infrastructure smoke test and
needs no Claude key, so you can diagnose scaling without spending tokens.
`ResearchWorkflow` is the real app.

---

## Configuration

Locally you can skip all of it — the defaults target `temporal server start-dev`.

| Variable | Default | Notes |
|---|---|---|
| `TEMPORAL_ADDRESS` | `localhost:7233` | `<ns>.<acct>.tmprl.cloud:7233` for Cloud |
| `TEMPORAL_API_KEY` | *unset* | Temporal Cloud; setting it turns TLS on |
| `TEMPORAL_DEPLOYMENT_NAME` | `research-fleet` | A mismatch makes Workflows hang with **no error** |
| `MAX_CONCURRENT_ACTIVITIES` | `1` | Activity slots per Worker. Raising it hides the scaling behaviour |
| `MAX_SUBQUESTIONS` | `6` | Fan-out width — this *is* the worker count one question lights up |
| `RESEARCH_EFFORT` | `medium` | The biggest lever on how long the room waits |
| `ANTHROPIC_API_KEY` | *unset* | Research app only; from Secret Manager in production |
| `DEMO_PASSCODE` | *unset* | Web tier. Empty means anyone with the URL can spend tokens |

Full list with reasoning lives in `runtime.py` and `research_activities.py`.

---

## Repo map

| | |
|---|---|
| `runtime.py` | The infra half — connect, versioned Worker, slot limits, SIGTERM drain. App-agnostic |
| `workflows.py` · `activities.py` | The hello app: the infrastructure smoke test |
| `llm.py` | The Claude seam. Imports no `temporalio`; `max_retries=0` because Temporal owns retry |
| `research_*.py` | Plan → fan out → draft → human review → optional second pass |
| `web.py` · `web/` | The single-page console. Vanilla JS, no build step |
| `terraform/` | The whole GCP stack, one `make up` |
| `tests/` · `learn/` · `docs/` | 111 offline tests · eleven in-app learn cards · tutorial and knowledge base |

---

## License

Apache-2.0 — see [LICENSE](LICENSE). Copyright 2026 Temporal Technologies, Inc.

Bundled fonts are redistributed under the SIL Open Font License
(`web/fonts/LICENSE-*.txt`). The Temporal wordmark in `web/temporal-logo.svg` is a
trademark of Temporal Technologies, included for use in this demo and not covered by
Apache-2.0.
