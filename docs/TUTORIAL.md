# Research Fleet — A Serverless Workers POC on GCP Cloud Run

The manual walkthrough behind `make up`. One Workflow, one 5-second Activity;
the interesting part is that the Worker lives in a Cloud Run Worker Pool that
Temporal scales from zero. Burst some Workflows and watch it grow.

> **Most of this is now automated.** `../terraform` does all of it in one apply —
> read that first unless you specifically want to understand each step.

---

> **New here?** Read `../README.md` first and get the local loop working. This file
> is only about *where the Worker runs*.

## 0. Before you start

**Serverless Workers are in Pre-release, and the Cloud Run compute provider is
not in a released CLI yet.** Two separate gates:

1. **CLI.** The `--gcp-cloud-run-*` flags used in Step 6 exist only on the `main`
   branch of `temporalio/cli`. Released versions (v1.7.1, v1.8.1) have
   `--aws-lambda-*` only. Build from source (needs Go):
   ```bash
   git clone --depth 1 https://github.com/temporalio/cli.git
   cd cli && go build -o temporal-main ./cmd/temporal
   ./temporal-main worker deployment create-version --help   # verify the flags
   ```
   Alternatively, drive Steps 5–6 entirely through the Temporal Cloud UI.
2. **Server.** Temporal *Cloud* needs Pre-release access granted on your
   Namespace. A **self-hosted** server does not — the server bundled with the
   CLI built above already supports the provider. See "Self-hosted alternative"
   at the bottom.

For the Temporal Cloud route:
- Request access via a [Temporal Cloud support ticket](https://docs.temporal.io/cloud/support#support-ticket) or your account team
- A **Temporal Cloud account with a GCP-hosted Namespace** (this tutorial assumes Cloud; self-hosted needs [additional setup](https://docs.temporal.io/production-deployment/worker-deployments/serverless-workers/self-hosted-setup) and Temporal Service v1.31.0+)
- Your Namespace should be configured for **API key authentication**

You'll also need:
- `gcloud` CLI, authenticated, with your project set:
  ```bash
  gcloud config set project serverless-workers-demo
  ```
- `terraform` CLI
- Python 3.12+ and the [Temporal CLI](https://docs.temporal.io/cli#installation) (`brew install temporal`, or see the archive for Linux)
- Your Temporal Cloud API key (Namespace → **API Keys** in the Temporal Cloud UI)

Everything in **Steps 1–2** works with zero GCP or Temporal Cloud involvement
— do those first to make sure the workflow logic itself is right before
spending time on cloud plumbing.

---

## 1. Sanity-check the workflow locally

No GCP, no Temporal Cloud — just the Temporal dev server.

```bash
cd research-fleet
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# terminal 1
temporal server start-dev
# Web UI at http://localhost:8233

# terminal 2 — one-time per dev server, then the Worker.
# HelloWorkflow declares PINNED, so the Worker must run versioned, and a
# versioned Task Queue needs a current version to dispatch to.
temporal worker deployment set-current-version \
    --deployment-name research-fleet --build-id local --yes
.venv/bin/python worker_local.py

# terminal 3 — no env vars needed; defaults point at the dev server
.venv/bin/python starter.py --watch
```

You should see `→ started hello-…` immediately, then `✅ Hello, world!` about 5
seconds later. Check `http://localhost:8233` for the event history.

Once this works, the workflow logic is proven — everything from here on is
purely about *where* the Worker runs.

---

## 2. GCP project setup

```bash
export PROJECT_ID=serverless-workers-demo
export REGION=us-central1

gcloud config set project $PROJECT_ID

gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  --project=$PROJECT_ID

gcloud artifacts repositories create research-fleet-repo \
  --repository-format=docker \
  --location=$REGION \
  --project=$PROJECT_ID
```

### Runtime service account (what the Worker's own container runs as)

```bash
gcloud iam service-accounts create research-worker-runtime \
  --display-name="Research Fleet — Worker runtime" \
  --project=$PROJECT_ID

export RUNTIME_SA=research-worker-runtime@$PROJECT_ID.iam.gserviceaccount.com
```

### Store your Temporal API key in Secret Manager

```bash
echo -n "<your-temporal-api-key>" | gcloud secrets create temporal-api-key \
  --data-file=- \
  --project=$PROJECT_ID

gcloud secrets add-iam-policy-binding temporal-api-key \
  --member="serviceAccount:$RUNTIME_SA" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$PROJECT_ID
```

---

## 3. Containerize and push the Worker

From the `research-fleet/` root — the `Dockerfile` sits there and copies its
three source files as siblings.

**Recommended: build locally and push.** Fewer moving parts than Cloud Build, and
no extra IAM to grant.

```bash
gcloud auth configure-docker $REGION-docker.pkg.dev

IMG=$REGION-docker.pkg.dev/$PROJECT_ID/research-fleet-repo/research-worker:v1
docker build --platform linux/amd64 -t "$IMG" .
docker push "$IMG"
```

> ⚠️ **`--platform linux/amd64` is not optional on an Apple Silicon Mac.** A plain
> `docker build` produces an arm64 image, Cloud Run runs amd64, and the pool will
> fail to start instances with an exec-format error that doesn't obviously point
> back here. Verify with:
> ```bash
> docker manifest inspect "$IMG" | grep -A2 platform
> ```

<details>
<summary>Alternative: <code>gcloud builds submit</code> (and the 403 you'll probably hit)</summary>

```bash
gcloud builds submit \
  --tag $REGION-docker.pkg.dev/$PROJECT_ID/research-fleet-repo/research-worker:v1 \
  --project=$PROJECT_ID --region=$REGION .
```

On a recently created project this fails with:

```
INVALID_ARGUMENT: could not resolve source: Error 403:
<project-number>-compute@developer.gserviceaccount.com does not have
storage.objects.get access to the Google Cloud Storage object
```

Cloud Build runs as the **Compute Engine default service account**, which no
longer gets the necessary roles automatically. To use this path, grant them:

```bash
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
CB_SA=$PROJECT_NUMBER-compute@developer.gserviceaccount.com
for role in roles/storage.objectAdmin roles/logging.logWriter roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$CB_SA" --role="$role"
done
```

Note `gcloud builds submit` respects `.gcloudignore`, and falls back to
`.gitignore` when there isn't one — which is what keeps `.venv/` out of the
upload here. It does **not** read `.dockerignore` (that only applies to
`docker build`).

</details>

---

## 4. Create the Cloud Run Worker Pool (start empty)

```bash
gcloud run worker-pools deploy research-worker-pool \
  --image $REGION-docker.pkg.dev/$PROJECT_ID/research-fleet-repo/research-worker:v1 \
  --region $REGION \
  --project $PROJECT_ID \
  --service-account $RUNTIME_SA \
  --instances 0 \
  --set-env-vars TEMPORAL_ADDRESS=<your-namespace>.<account>.tmprl.cloud:7233,TEMPORAL_NAMESPACE=<your-namespace>.<account>,TEMPORAL_TASK_QUEUE=research-queue,BUILD_ID=v1 \
  --set-secrets TEMPORAL_API_KEY=temporal-api-key:latest
```

It's fine — expected, even — that this pool does nothing yet. It has zero
instances until Temporal tells it to scale up.

---

## 5. Give Temporal permission to scale the pool

In the **Temporal Cloud UI**: open your Namespace → **Workers** → **Create
Worker Deployment**. Start filling in Compute Provider = **Google Cloud
Run** — it will show you an `impersonator_service_account_emails` value to
use. Copy it, then:

```bash
cd terraform
terraform init
terraform apply -var="project_id=serverless-workers-demo" \
  -var='impersonator_service_account_emails=["<value-from-the-ui>"]'
```

Note the `invoker_email` output — you need it next. (Run `terraform output`
if you don't see it printed.)

If you're running a **self-hosted** server that authenticates with your own
user-account ADC, there is no Temporal Cloud identity to paste. Grant yourself
impersonation instead:

```bash
terraform apply -var="project_id=serverless-workers-demo" \
  -var='impersonator_user_emails=["you@example.com"]'
```

---

## 6. Wire up the Worker Deployment Version

Back in the Temporal Cloud UI (or via CLI):

Use the CLI you built in Step 0 — released versions don't have these flags.

```bash
./temporal-main worker deployment create-version \
  --namespace <your-namespace>.<account> \
  --deployment-name research-fleet \
  --build-id v1 \
  --gcp-cloud-run-project serverless-workers-demo \
  --gcp-cloud-run-region us-central1 \
  --gcp-cloud-run-worker-pool research-worker-pool \
  --gcp-cloud-run-service-account <invoker-email-from-step-5>

./temporal-main worker deployment set-current-version \
  --namespace <your-namespace>.<account> \
  --deployment-name research-fleet \
  --build-id v1
```

`create-version` requires the Worker Deployment to already exist, i.e. something
must have polled `research-queue` under deployment name `research-fleet` at least
once. If you get `no Worker Deployment found with name 'research-fleet'`, that's
what it means.

(If you used the Temporal Cloud UI end-to-end instead, the version is set current automatically on save.)

---

## 7. Play the game

```bash
export TEMPORAL_ADDRESS=<your-namespace>.<account>.tmprl.cloud:7233
export TEMPORAL_NAMESPACE=<your-namespace>.<account>
export TEMPORAL_API_KEY=<your-temporal-api-key>
export TEMPORAL_TASK_QUEUE=research-queue

.venv/bin/python starter.py --watch
```

First run will take a few seconds longer than local — that's a Cloud Run
instance actually starting from zero. Check it happened:

```bash
gcloud run worker-pools describe research-worker-pool \
  --region us-central1 --project serverless-workers-demo

gcloud run worker-pools logs read research-worker-pool \
  --region us-central1 --project serverless-workers-demo
```

### Burst mode — the actual "demo moment"

```bash
.venv/bin/python starter.py --count 20
```

Then re-run the `describe` command above every few seconds and watch the
instance count climb, hold, then come back down once the queue's been quiet
for a while.

---

## 8. Cleanup (avoid leaving billable resources on)

```bash
gcloud run worker-pools delete research-worker-pool --region us-central1 --project serverless-workers-demo
gcloud secrets delete temporal-api-key --project serverless-workers-demo
gcloud artifacts repositories delete research-fleet-repo --location us-central1 --project serverless-workers-demo
cd terraform && terraform destroy
```

Also delete the Worker Deployment / Worker Deployment Version from the
Temporal Cloud UI if you don't plan to reuse it.

---

## Self-hosted alternative (no Pre-release access needed)

The Pre-release gate is **Temporal Cloud-side only**. The server bundled with the
CLI you built in Step 0 (1.32.0+) already supports the Cloud Run compute
provider, so you can do all of Steps 5–6 against your own server.

Enable the Worker Controller via dynamic config. Note it's repeated
`--dynamic-config-value` flags — there is no `--dynamic-config-file`:

```bash
./temporal-main server start-dev \
  --dynamic-config-value workercontroller.enabled=true \
  --dynamic-config-value 'workercontroller.compute_providers.enabled=["gcp-cloud-run"]' \
  --dynamic-config-value 'workercontroller.scaling_algorithms.enabled=["no-sync"]'
```

Give the server a GCP identity so it can impersonate the invoker SA:

```bash
gcloud auth application-default login
```

Without that, `create-version` fails with `could not find default credentials`.

**The one real catch:** Cloud Run pool instances have to reach your Temporal
frontend. `localhost:7233` is not reachable from Cloud Run, so a laptop server
needs a tunnel (`cloudflared`, `ngrok`) with `TEMPORAL_ADDRESS` on the pool
pointed at the public hostname — or run the server on GCE/GKE in the same
project. See KNOWLEDGE_BASE.md §7.6 for the networking requirements.

---

## Troubleshooting

- **No logs at all** — the pool is at 0 instances and nothing has triggered
  a scale-up yet. That's normal before your first `starter.py` run.
- **First workflow hangs for a while, then completes** — expected; that's
  cold start. Subsequent bursts within the same warm period will feel faster.
- **`Validate Connection` fails in the Temporal UI** — double check the
  invoker email from Step 5 actually matches what you registered in Step 6,
  and that the runtime service account in Step 4 has the Secret Manager
  accessor role.
- Full reference: [Troubleshoot Serverless Workers](https://docs.temporal.io/troubleshooting/serverless-workers)
