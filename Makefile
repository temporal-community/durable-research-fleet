# Research Fleet — one-command stack.
#
#   make up         build+push the image, then apply the whole stack
#   make down       destroy everything Terraform owns
#   make web-local  Phase 0: the research agent against a local dev server
#   make test       everything offline (pytest + terraform test)
#
# Terraform owns all GCP resources. It does NOT build container images, so the
# image build/push is a separate step that `make up` sequences for you.
#
# ONE image serves both tiers: the Worker Pool runs its default command, and the
# Cloud Run Service overrides it to run uvicorn. So `make image` feeds both, and
# there is no way for them to drift onto different code.
#
# After `make up`, get the URLs and the two commands you'll want on stage:
#   terraform -chdir=terraform output web_service_url  # internal-ingress; NOT a public link
#   terraform -chdir=terraform output set_gemini_key
#   terraform -chdir=terraform output chaos
#
# The console itself is served by `make web-local` over the SSH tunnel — the Cloud Run
# Service is not internet-reachable on purpose (AGENTS.md gate #12).

# SET THIS. The default is the project this demo was built in, and you almost
# certainly cannot deploy into it:
#   make up AUTO=1 PROJECT=your-project-id
PROJECT ?= serverless-workers-demo
REGION  ?= us-east4
ZONE    ?= us-east4-a
PREFIX  ?= research-fleet
BUILD_ID ?= v1
# Kept as its own variable rather than derived from PREFIX: the infra identifiers say
# research-fleet/research-queue and renaming them forces resource recreation for no gain.
TASK_QUEUE ?= research-queue
# Bump if a redeploy dies on "role_id (workerPoolScaler) ... marked for deletion":
# GCP reserves a deleted custom role's id for 37 days (terraform/variables.tf).
SCALER_ROLE_SUFFIX ?= 3
GEMINI_MODEL ?= gemini-3.6-flash
ANTHROPIC_MODEL ?= claude-opus-5

# CLI main currently pins a July auto-scaled-workers revision whose GCP provider
# sends the camelCase field mask scaling.manualInstanceCount over gRPC. Cloud Run
# accepts that request but silently leaves the instance count unchanged. This
# upstream revision carries the snake_case field-mask fix and its regression test.
AUTO_SCALED_WORKERS_VERSION ?= v0.0.0-20260811170210-91f6fe1d10ab

# Local ports for the SSH tunnels. Defaults match Temporal's conventions so
# starter.py works with no env vars — but override them if something else is
# already bound (a local dev server, another compose stack):
#   make tunnel LOCAL_FRONTEND_PORT=7433
LOCAL_FRONTEND_PORT ?= 7233
LOCAL_UI_PORT       ?= 8233
# The console's own port. 8000 is a popular default (uvicorn, http.server, plenty of
# dev servers), so this is overridable for the same reason the tunnel ports are:
#   make web-local WEB_PORT=8100
WEB_PORT            ?= 8000

IMAGE := $(REGION)-docker.pkg.dev/$(PROJECT)/$(PREFIX)-repo/research-worker:$(BUILD_ID)
TF    := terraform -chdir=terraform

# Every Make override must reach Terraform too. Forwarding only project_id means
# `make up BUILD_ID=v2` pushes :v2 while the pool still points at :v1, and
# `make up REGION=...` creates the repo in one region and pushes to another.
# deployment_name and task_queue are forwarded too, and that is not cosmetic:
# var.deployment_name defaults to the literal "research-fleet" INDEPENDENTLY of
# name_prefix, and it is what the pool exports as TEMPORAL_DEPLOYMENT_NAME. Without
# these, `make up PREFIX=demo2` registers workers under "research-fleet" while
# `make status` and scripts/verify_stack.sh query "demo2" — verify then reports "no
# compute config", "no Task Queues" and "not current" on a stack that is actually
# healthy, which is the worst possible false alarm minutes before a talk.
TF_VARS := \
  -var="project_id=$(PROJECT)" \
  -var="region=$(REGION)" \
  -var="zone=$(ZONE)" \
  -var="name_prefix=$(PREFIX)" \
  -var="deployment_name=$(PREFIX)" \
  -var="task_queue=$(TASK_QUEUE)" \
  -var="build_id=$(BUILD_ID)" \
  -var="gemini_model=$(GEMINI_MODEL)" \
  -var="anthropic_model=$(ANTHROPIC_MODEL)" \
  -var="scaler_role_suffix=$(SCALER_ROLE_SUFFIX)"

# Fail fast rather than half-deploying on a mismatch.
#
# The project check exists because PROJECT defaults to the project this demo was built
# in. Anyone cloning the repo and running `make up` verbatim aimed at it and got a
# permission error from somewhere deep in an apply, with nothing pointing at the cause.
.PHONY: check-config
check-config:
	@case "$(ZONE)" in $(REGION)-*) ;; *) \
	  echo "ERROR: ZONE=$(ZONE) is not in REGION=$(REGION)"; exit 1 ;; esac
	@gcloud projects describe $(PROJECT) >/dev/null 2>&1 || { \
	  echo "ERROR: cannot access PROJECT=$(PROJECT)."; \
	  echo "       The default is the project this demo was built in — set your own:"; \
	  echo "         make $(MAKECMDGOALS) PROJECT=your-project-id"; \
	  exit 1; }

.PHONY: ac-build ac-init ac-plan ac-apply ac-secrets ac-register ac-verify ac-down ac-prune web-cloud test-ac help up down image cli plan apply destroy repo bootstrap-log ui tunnel status fmt check-config adopt register-queues test test-py test-tf verify web-local

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t22

## ---------------------------------------------------------------------------

up: check-config cli repo image secret apply ## Full stack from nothing
	@echo ""
	@echo "Stack up. Next:"
	@$(TF) output -raw bootstrap_log; echo ""

# Cloud Run holds an address reservation in the subnet after the pool is deleted
# and releases it asynchronously, so the subnet/VPC can survive the first pass.
# Everything else is destroyed; re-run later to finish. Safe to run repeatedly.
down: ## Destroy everything (re-run later if the subnet is still held — see below)
	@$(TF) destroy $(TF_VARS) $(APPROVE) || { \
	  echo ""; \
	  echo "=========================================================="; \
	  echo " Partial teardown. Almost certainly this:"; \
	  echo "   Cloud Run still holds a serverless-ipv4-* address"; \
	  echo "   reservation in the subnet, which it releases on its own"; \
	  echo "   schedule. The address cannot be deleted by hand."; \
	  echo ""; \
	  echo " Everything else is gone. Only the subnet + VPC remain, and"; \
	  echo " they cost nothing. Re-run 'make down' later to finish."; \
	  echo ""; \
	  echo " Check with:"; \
	  echo "   gcloud compute addresses list --project $(PROJECT)"; \
	  echo "=========================================================="; \
	  exit 1; }

## ---------------------------------------------------------------------------

cli: bin/temporal-linux ## Build the main-branch temporal CLI (linux/amd64)

bin/temporal-linux:
	@echo "==> building temporal CLI from main (the Cloud Run provider is not in any release)"
	@mkdir -p bin
	@rm -rf /tmp/bb-cli && git clone --depth 1 https://github.com/temporalio/cli.git /tmp/bb-cli
	cd /tmp/bb-cli && go get go.temporal.io/auto-scaled-workers@$(AUTO_SCALED_WORKERS_VERSION)
	cd /tmp/bb-cli && GOOS=linux GOARCH=amd64 go build -o $(CURDIR)/bin/temporal-linux ./cmd/temporal
	@echo "==> $$($(CURDIR)/bin/temporal-linux --version 2>/dev/null || echo built)"

# Artifact Registry must exist before the image can be pushed, but the pool
# needs the image at create time — so create just the repo first.
repo: check-config ## Create only the Artifact Registry repo (so the image can be pushed)
	$(TF) init -upgrade
	$(TF) apply -target=google_artifact_registry_repository.repo $(TF_VARS) -auto-approve

image: ## Build linux/amd64 image and push
	@echo "==> --platform linux/amd64 is required; an arm64 Mac build will not run on Cloud Run"
	gcloud auth configure-docker $(REGION)-docker.pkg.dev --quiet
	docker build --platform linux/amd64 -t $(IMAGE) .
	docker push $(IMAGE)

# The pool mounts both provider keys as secret refs pinned to "latest", and Cloud
# Run resolves them AT POOL CREATE TIME. If either secret has no versions it
# refuses to create the pool at all:
#
#   Error code 9: spec.template.spec.containers[0].env[8].value_from.secret_key_ref
#   .name: Secret .../secrets/research-fleet-gemini-api-key/versions/latest was not found
#
# So the secret must exist AND hold a version before the main apply. Found on the
# first real deploy, 2026-07-29 — `make up` could never have worked from scratch,
# because Terraform creates the secret empty by design (the key must not enter
# state — decisions/05 D-5.11) and then creates the pool in the same apply.
#
# Same idiom as `repo` above: one -target apply to break a create-time ordering
# cycle a single apply cannot express. Idempotent — it never clobbers an existing
# version, because doing so would repoint "latest" at a placeholder on a live stack.
secret: check-config ## Create both provider secrets and seed versions (must precede the pool)
	$(TF) init -upgrade
	$(TF) apply -target=google_secret_manager_secret.gemini \
	  -target=google_secret_manager_secret.anthropic $(TF_VARS) -auto-approve
	@if gcloud secrets versions list $(PREFIX)-gemini-api-key --project=$(PROJECT) \
	      --filter='state=enabled' --format='value(name)' 2>/dev/null | grep -q .; then \
	  echo "==> secret already holds an enabled version; leaving it untouched"; \
	elif [ -n "$$GEMINI_API_KEY" ]; then \
	  printf %s "$$GEMINI_API_KEY" | gcloud secrets versions add \
	    $(PREFIX)-gemini-api-key --data-file=- --project=$(PROJECT) >/dev/null; \
	  echo "==> stored GEMINI_API_KEY (never passed through Terraform)"; \
	else \
	  printf %s unset | gcloud secrets versions add \
	    $(PREFIX)-gemini-api-key --data-file=- --project=$(PROJECT) >/dev/null; \
	  echo "==> WARNING: GEMINI_API_KEY unset — seeded a placeholder so the pool can"; \
	  echo "    be created. The hello app and 'make verify SCALE=1' work as documented;"; \
	  echo "    research Activities will fail until you run:"; \
	  echo "      make -s print-set-key"; \
	fi
	@if gcloud secrets versions list $(PREFIX)-anthropic-api-key --project=$(PROJECT) \
	      --filter='state=enabled' --format='value(name)' 2>/dev/null | grep -q .; then \
	  echo "==> Anthropic secret already holds an enabled version; leaving it untouched"; \
	elif [ -n "$$ANTHROPIC_API_KEY" ]; then \
	  printf %s "$$ANTHROPIC_API_KEY" | gcloud secrets versions add \
	    $(PREFIX)-anthropic-api-key --data-file=- --project=$(PROJECT) >/dev/null; \
	  echo "==> stored ANTHROPIC_API_KEY (never passed through Terraform)"; \
	else \
	  printf %s unset | gcloud secrets versions add \
	    $(PREFIX)-anthropic-api-key --data-file=- --project=$(PROJECT) >/dev/null; \
	  echo "==> ANTHROPIC_API_KEY unset — seeded a placeholder; Gemini remains usable"; \
	fi

print-set-key: ## Print commands that store real provider keys
	@$(TF) output -raw set_gemini_key; echo ""
	@$(TF) output -raw set_anthropic_key; echo ""

plan: check-config ## Show the plan
	$(TF) init -upgrade
	$(TF) plan $(TF_VARS)

# AUTO=1 skips the interactive plan approval. `make up` and `make down` NEED it in any
# non-interactive shell: without it terraform dies on "error asking for approval: EOF",
# and because `up` depends on `apply`, `make up` can never complete unattended. Off by
# default so a human still sees the plan before it runs.
APPROVE := $(if $(AUTO),-auto-approve,)

apply: check-config ## Apply the stack (AUTO=1 to skip the approval prompt)
	$(TF) init -upgrade
	$(TF) apply $(TF_VARS) $(APPROVE)

# Only for migrating an environment where the repo/VPC/subnet already exist with
# this stack's names. Imports are off by default so a clean project can apply.
adopt: check-config ## One-time: import pre-existing repo/VPC/subnet into state
	$(TF) init -upgrade
	$(TF) apply $(TF_VARS) -var="adopt_existing=true"

## ---------------------------------------------------------------------------

# A pre-defined Worker Deployment Version has a compute config but no Task
# Queues until a Worker actually polls, and the WCI needs that association to
# know what to watch. Run once after a fresh `make up`.
register-queues: ## One-time after first apply: let the pool poll once so Task Queues attach
	@echo "==> scaling to 1 so the Version learns its Task Queues"
	gcloud run worker-pools update $(PREFIX)-worker-pool --instances 1 --region $(REGION) --project $(PROJECT)
	@echo "==> waiting for the Worker to poll..."
	@sleep 45
	gcloud compute ssh $(PREFIX)-temporal --zone $(ZONE) --project $(PROJECT) --quiet --command \
	  '/opt/temporal/temporal --address 127.0.0.1:7233 -n default worker deployment describe-version \
	     --deployment-name $(PREFIX) --build-id $(BUILD_ID) | tail -6'
	@echo "==> back to 0; the Worker Controller owns it from here"
	gcloud run worker-pools update $(PREFIX)-worker-pool --instances 0 --region $(REGION) --project $(PROJECT)

## --- local development (Phase 0) ------------------------------------------

# The whole research app runs locally with no GCP and no Pre-release access, which
# is the point: get the agent right before the namespace gate matters.
#
#   1. temporal server start-dev
#   2. export GEMINI_API_KEY=...          (or put it in .env, which is gitignored)
#   3. MAX_CONCURRENT_ACTIVITIES=6 .venv/bin/python worker_local.py
#   4. temporal worker deployment set-current-version \
#        --deployment-name research-fleet --build-id local --yes
#   5. make web-local          -> http://localhost:8000
#
# Two things about step 3 and 4:
#   - set-current-version comes AFTER the Worker: the Worker Deployment does not
#     exist until something polls it.
#   - raise the slot count LOCALLY only. The deployed default of 1 is what makes one
#     question light up six Serverless Workers; locally there is no Worker Controller
#     to scale, so slot=1 just serialises six multi-minute Activities for no gain.
web-local: ## Serve the research console (WEB_PORT, against LOCAL_FRONTEND_PORT)
	$(call check_port,$(WEB_PORT),web-local,WEB_PORT)
	@echo "==> http://localhost:$(WEB_PORT)   (phone and projector — one page)"
	@echo "    talking to Temporal at localhost:$(LOCAL_FRONTEND_PORT)"
	TEMPORAL_ADDRESS=$${TEMPORAL_ADDRESS:-localhost:$(LOCAL_FRONTEND_PORT)} \
	  .venv/bin/python -m uvicorn web:app --reload --port $(WEB_PORT) --no-proxy-headers

## --- tests ---------------------------------------------------------------

test: test-py test-tf test-ac ## Run every offline test (no cloud resources touched)

test-py: ## pytest: workflow, activity, replay and Serverless-contract tests
	.venv/bin/python -m pytest

test-tf: ## terraform test: plan-only assertions on the stack wiring
	$(TF) test

test-ac: ## terraform test for the AgentCore stack (mocked provider; no AWS calls)
	cd agentcore && terraform init -backend=false -input=false >/dev/null && terraform test

verify: ## Verify the LIVE stack (add SCALE=1 for the full scale-from-zero cycle)
	@PROJECT=$(PROJECT) REGION=$(REGION) ZONE=$(ZONE) PREFIX=$(PREFIX) \
	  BUILD_ID=$(BUILD_ID) TASK_QUEUE=$(TASK_QUEUE) SCALE=$${SCALE:-0} \
	  ./scripts/verify_stack.sh

## --- operations ----------------------------------------------------------

status: ## Pool instance count + VM state
	@gcloud run worker-pools describe $(PREFIX)-worker-pool --region $(REGION) --project $(PROJECT) 2>/dev/null | grep -i 'Scaling:' || echo "pool not found"
	@gcloud compute instances describe $(PREFIX)-temporal --zone $(ZONE) --project $(PROJECT) --format='value(status)' 2>/dev/null || echo "vm not found"

bootstrap-log: ## Tail the VM bootstrap log
	gcloud compute ssh $(PREFIX)-temporal --zone $(ZONE) --project $(PROJECT) --command 'sudo tail -f /var/log/bb-bootstrap.log'

# Fail with a useful message instead of ssh's bare "Address already in use".
define check_port
	@lsof -nP -iTCP:$(1) -sTCP:LISTEN >/dev/null 2>&1 && { \
	  echo "ERROR: local port $(1) is already in use."; \
	  echo "       Pick another:  make $(2) $(3)=<port>"; \
	  exit 1; } || true
endef

# A bare `gcloud compute ssh -L` tunnel is a single TCP connection with no
# keepalive, and it dies for reasons that have nothing to do with the demo:
# a VPN re-handshake, a laptop sleeping, a network switch. When it dies the
# console keeps serving pages while every Temporal call fails, which reads on
# stage as "the app is broken" rather than "the tunnel dropped".
#
# So: ServerAliveInterval notices a dead peer in ~45s instead of hanging, and
# the while-loop re-dials. ExitOnForwardFailure matters more than it looks --
# without it ssh happily connects while silently failing to bind the local
# port, so you get a live session forwarding nothing.
#
# The trade-off is that Ctrl-C now has to break the loop, not just ssh, hence
# the explicit trap.
define ssh_tunnel
	@echo "==> $(3) on localhost:$(1) — leave this running (auto-reconnects)"
	@trap 'echo; echo "==> tunnel closed"; exit 0' INT; \
	while true; do \
	  gcloud compute ssh $(PREFIX)-temporal --zone $(ZONE) --project $(PROJECT) -- -N \
	    -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
	    -L $(1):localhost:$(2) || true; \
	  echo "    [$$(date +%H:%M:%S)] tunnel on $(1) dropped — reconnecting in 5s"; \
	  sleep 5; \
	done
endef

tunnel: ## Forward the frontend to localhost:$(LOCAL_FRONTEND_PORT) (starter.py then works unchanged)
	$(call check_port,$(LOCAL_FRONTEND_PORT),tunnel,LOCAL_FRONTEND_PORT)
	@echo "    TEMPORAL_ADDRESS=localhost:$(LOCAL_FRONTEND_PORT) .venv/bin/python starter.py --watch"
	$(call ssh_tunnel,$(LOCAL_FRONTEND_PORT),7233,frontend)

ui: ## Forward the Temporal Web UI to http://localhost:$(LOCAL_UI_PORT)
	$(call check_port,$(LOCAL_UI_PORT),ui,LOCAL_UI_PORT)
	$(call ssh_tunnel,$(LOCAL_UI_PORT),8233,Temporal UI)

fmt:
	$(TF) fmt

## --- AgentCore on Temporal Cloud (see docs/AGENTCORE.md) -------------------------------
#
# A second deployment target, beside the GCP/Cloud Run stack above. The Worker runs in an
# Amazon Bedrock AgentCore Runtime session and Temporal Cloud invokes it. Order:
#   make ac-build ac-apply ac-secrets ac-register ac-verify
# Everything reads agentcore/terraform.tfvars (gitignored) for the Namespace.
AC := terraform -chdir=agentcore

ac-build: ## Build the arm64 Runtime zip (deterministic, so no spurious versions)
	./agentcore/build.sh

ac-init:
	$(AC) init -input=false

ac-plan: ac-init ac-build ## Show the AWS plan
	./agentcore/guard.sh
	$(AC) plan -input=false

ac-apply: ac-init ac-build ## Apply the AWS stack (AUTO=1 skips the approval prompt)
	./agentcore/guard.sh
	$(AC) apply $(if $(AUTO),-auto-approve,)

ac-secrets: ## Seed Secrets Manager from .env (never overwrites; FORCE=1 to replace)
	./agentcore/seed-secrets.sh

ac-register: ## Register this build's endpoint with Temporal Cloud (SET_CURRENT=1 to route to it)
	./agentcore/register.sh

ac-verify: ## Run a real Workflow end to end on AgentCore and check the fleet
	./agentcore/verify.sh

ac-prune: ## READ-ONLY: show each Version's drainage; only drained ones may leave retained_build_ids
	@test -f .env || { echo ".env is required (TEMPORAL_API_KEY)"; exit 1; }
	@set -a; . ./.env; set +a; \
	  NS=$$($(AC) output -raw temporal_namespace); \
	  TEMPORAL_ADDRESS=$$NS.tmprl.cloud:7233 TEMPORAL_NAMESPACE=$$NS \
	  temporal worker deployment describe --name $$($(AC) output -raw deployment_name)

ac-down: ## Destroy the AWS stack (the Temporal Deployment/Version is left; unset it yourself)
	$(AC) destroy $(if $(AUTO),-auto-approve,)

web-cloud: ## Serve the console against the Temporal Cloud Namespace in .env
	$(call check_port,$(WEB_PORT),web-cloud,WEB_PORT)
	@set -a; . ./.env; set +a; \
	  TEMPORAL_ADDRESS=$$($(AC) output -raw temporal_namespace).tmprl.cloud:7233 \
	  TEMPORAL_NAMESPACE=$$($(AC) output -raw temporal_namespace) \
	  TEMPORAL_TASK_QUEUE=$(TASK_QUEUE) \
	  .venv/bin/python -m uvicorn web:app --port $(WEB_PORT) --no-proxy-headers
