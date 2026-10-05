# Running Research Fleet on Amazon Bedrock AgentCore (Temporal Cloud)

> To compare AgentCore with Cloud Run, see "Choose your platform" in the [README](../README.md#-choose-your-platform). This page is the full technical write-up.

A second deployment target beside the GCP/Cloud Run stack. Same Workflows, same
Activities, same `runtime.py`; only the compute provider and the Temporal topology
change. **Pre-release** on Temporal's side: needs the AgentCore Serverless Workers grant
on an **AWS-hosted Temporal Cloud Namespace** (not self-hosted; the self-hosted setup
docs list only `aws-lambda`).

## How it works

```
 starter / web.py ──start──▶ Temporal Cloud (ca-central-1) ◀──polls── Worker in a session
                                  │ backlog on research-queue                  ▲
                                  ▼                                            │ starts
                       Worker Controller Instance ──InvokeAgentRuntime──▶ AgentCore Runtime
                          (assumes the invoker role,                      endpoint  b_<build>
                           ExternalId-gated)                              (pinned to one version)
```

- Temporal **invokes** an endpoint; each invocation starts a session (a microVM, up to 8h).
  `worker_agentcore.py` returns from `/invocations` immediately and polls in the
  background; `/ping` reports `HealthyBusy` for as long as the Worker runs.
- After `AGENTCORE_DEBOUNCE_SECONDS` (60) with no Activity running or finishing, the Worker
  drains and **releases the session**. Forgetting that pins it busy until `max_lifetime`.
- **One Build ID = one Runtime version = one endpoint.** Workflows are PINNED, so a
  Workflow parked in human review resumes on the code it started on.

## Layout

| File | Role |
|---|---|
| `agentcore_runtime.py` | the AgentCore half of the runtime: session lifecycle, idle tracking, secrets. No SDK import, so tests are offline |
| `worker_agentcore.py` | the app binding (~40 lines), as `worker_cloudrun.py` is for Cloud Run |
| `agentcore/*.tf` | S3 code bucket, Runtime + one endpoint per build, IAM, Secrets Manager |
| `agentcore/build.sh` | deterministic arm64 zip |
| `agentcore/{seed-secrets,register,verify}.sh` | secrets, Temporal registration, live check |

## Deploy

```bash
# agentcore/terraform.tfvars is gitignored; set temporal_namespace, temporal_address, build_id.
make ac-plan                    # read-only; review it
make ac-apply                   # creates the AWS resources
make ac-secrets                 # seeds TEMPORAL_API_KEY / GEMINI_API_KEY / ANTHROPIC_API_KEY from .env
SET_CURRENT=1 make ac-register  # creates the Worker Deployment Version, routes new Workflows to it
make ac-verify                  # N hello Workflows through real invocations; no model tokens
make web-cloud                  # the console, against this Namespace
```

## Speed settings (1.0.3), all measured against a 4m36s Claude baseline

| Setting | Value | Why |
|---|---|---|
| `CLAUDE_MAX_SEARCHES` (code default) | 4 | uncapped, 6 sub-questions ran 66 searches; each can force another round |
| `SYSTEM_RESEARCH` search budget | "at most four searches" | the prompt and the tool cap must agree |
| `ANTHROPIC_RESEARCH_MODEL` | `claude-sonnet-5-5` | narrow lookups; planning and synthesis stay on `claude-opus-5` |
| `max_concurrent_activities` | 3 | removes the ~70s queue wait |

The search cap and prompt apply to every deployment that runs Claude, including Cloud Run. Set
`CLAUDE_MAX_SEARCHES=0` to remove the cap. Quality impact is unmeasured: compare one answer.

## Know before you change anything

- **Any change to the Runtime needs a new `build_id`.** That includes env-var-only changes
  (e.g. `debounce_seconds`) and any code change. The endpoint is pinned to its version on
  purpose, so an apply with the same `build_id` publishes a version nothing references.
  `agentcore/guard.sh` (run by `ac-plan`/`ac-apply`) refuses that case.
- **Scale-in is two timers, not one.** The Worker drains after `debounce_seconds` (60), but
  AgentCore keeps the microVM for `idle_runtime_session_timeout` (900) more. The 60s is when
  the Worker stops polling; the ~16 minutes is when the session is actually reclaimed.
- **`make ac-down` then `make ac-apply` within `secret_recovery_window_days` (7) fails** on
  the reserved secret names. Set it to 0 for a throwaway stack.
- **A Query needs a live Worker.** After sessions drain, a UI poll cold-starts one (~5s). The
  console therefore serves a run parked at review from memory (`web.py`, 120s TTL, dropped on
  `/decision`).

## Shipping a new build

1. Change `build_id` in `agentcore/terraform.tfvars` and add the OLD id to
   `retained_build_ids`. Without that, Terraform would drop the old endpoint and strand
   every Workflow pinned to it.
2. `make ac-apply ac-register`, then `SET_CURRENT=1 make ac-register` once verified.
3. Once the old Version shows **drained** (`make ac-prune`), remove it from
   `retained_build_ids`.

## Decisions worth keeping

- **Secrets are ARNs, not values.** The Runtime config is readable by anyone with
  `GetAgentRuntime`. Values live in Secrets Manager, are seeded out of band (never in
  Terraform state), and are resolved by the session's execution role. Only the Temporal
  key is required; a missing model key degrades one provider, not the whole Worker.
- **Invocation role**: trusts Temporal's published principals behind an `ExternalId`, and
  allows exactly `InvokeAgentRuntime` + `GetAgentRuntimeEndpoint` on this Runtime.
- **`register.sh` refuses any Namespace but the one the stack was applied for.**
- **`ignore_changes = [agent_runtime_version]` on endpoints** is the versioning guarantee.
- **`MAX_CONCURRENT_ACTIVITIES` is 3 on AgentCore** (it stays 1 on Cloud Run, where one slot per
  instance IS the demo). Temporal started 3-4 sessions for 5-6 sub-questions, so with 1 slot the
  rest queued 61-69s. Whether it then starts fewer sessions is unmeasured; watch peak Workers.

## Verified live, 2026-10-05 (an AWS-hosted Temporal Cloud Namespace, ca-central-1)

`make ac-verify` (hello app, no model tokens): 5 Workflows with **no Worker running**
completed in **35s** (first at 18s, which is the cold start).

- **Temporal Cloud really invokes the Runtime**, and the arm64 code zip imports and runs.
- **Secrets resolve from Secrets Manager** at session start (session log:
  `resolved from Secrets Manager: TEMPORAL_API_KEY, GEMINI_API_KEY, ANTHROPIC_API_KEY`).
- **Sessions drain and release**: `idle for 60s; drained and releasing the session`.
- **Identities are distinct per session** (`340@localhost-32f8bb5a`, `341@localhost-78c229cd`).
  The hostname IS `localhost` in a session, exactly as on Cloud Run, so the random suffix
  in `runtime.worker_identity()` is doing real work here too; do not remove it.
- **Fan-out is real but is the scaler's call.** 5 queued tasks were served by **2** sessions
  (3 Activities on one, 2 on the other), not 5. Do not promise "one question, N workers" on
  stage for AgentCore the way the Cloud Run demo does; measure the width you actually get.
- A versioned Task Queue's `describe` returns `pollers: null`; poller counts must come from
  Workflow history (`verify.sh` does).

## Research app, verified live 2026-10-05 (Gemini, through `make web-cloud`)

One question: 5 sub-questions, 14 grounded searches, 25 sources, 32,665 tokens, **0 failures**,
`Pinned` to `research-fleet-agentcore:1.0.0`, parked correctly at the human-review pause.
The 7 Activities ran on **3** distinct sessions (2 / 4 / 1). Two of them were both pid 340 on
`localhost`, told apart only by the random suffix, so that suffix is load-bearing here.
The Claude path has not been run on AgentCore yet (the Anthropic key was rotated).

## Still unverified

- SIGTERM behaviour when AgentCore reclaims a session mid-Activity, so whether
  `GRACEFUL_SHUTDOWN_SECONDS` is ever honoured.
- The Claude provider path on AgentCore.
