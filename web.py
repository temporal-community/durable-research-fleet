"""The web tier — the single-page research console.

Runs on a Cloud Run **Service**, not a Worker Pool: it is a Temporal *client* that
starts Workflows, reads Queries and sends Signals. Same image as the Worker, with
the command overridden to uvicorn.

MUST NOT import `research_workflow`, `llm` or `anthropic`. Workflows are started,
queried and signalled by NAME STRING so the web process needs no Claude key.
Enforced by tests/test_serverless_contract.py.

The Serverless Workers count is `DescribeTaskQueue` poller identities — one Worker
per Cloud Run instance, so distinct identities IS the count. One source rather than
the Cloud Run Admin API so the number is produced identically locally and on stage.
Known tradeoff: poller entries age out over ~5 minutes, so the count lags scale-IN
while scale-out is immediate.

There is deliberately no chaos/scale endpoint — that would need Cloud Run write
permission on a page anyone in the room can POST to.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

logger = logging.getLogger("web")

WORKFLOW_NAME = "ResearchWorkflow"
STATIC = Path(__file__).parent / "web"

ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
TASK_QUEUE = os.environ.get("TEMPORAL_TASK_QUEUE", "research-queue")

# Guards on a page anyone in the room can reach. A question starts a Workflow that
# spends money, so both of these are load-bearing rather than hygiene.
MAX_QUESTION_CHARS = 500
ASKS_PER_MINUTE_PER_IP = 3
# Shared passcode announced from the stage. Unset means open.
# CASE-INSENSITIVE on purpose: phone keyboards auto-capitalise the first letter, so a
# case-sensitive check rejects most of the room on their first try.
DEMO_PASSCODE = os.environ.get("DEMO_PASSCODE", "").strip()


def _passcode_ok(supplied: str | None) -> bool:
    if not DEMO_PASSCODE:
        return True
    # compare_digest, not `==`. The timing signal on a short shared passcode over
    # HTTPS is not a practical attack, but this is a public repo people copy from and
    # `==` on a secret is the wrong thing to hand them.
    return secrets.compare_digest(
        (supplied or "").strip().casefold(), DEMO_PASSCODE.casefold()
    )

app = FastAPI(title="Research Agent on Temporal Serverless Workers")

_client: Client | None = None
_asks: dict[str, list[float]] = {}

# Derived from watching the live number change; resets on restart, which is the right
# scope for a demo.
_peak_workers = 0
_total_started = 0
_last_workers = 0

# Memo for /api/fleet. Every phone renders the counter and each uncached read costs
# 3 RPCs, so 20 phones would be ~30 RPC/s against a single-VM Temporal — enough to
# degrade the thing the counter displays. The LOCK matters: without it N concurrent
# requests on a cold cache all miss and all fan out, which is the stampede this
# exists to prevent.
_fleet_cache: dict | None = None
_fleet_cached_at = 0.0
_fleet_lock = asyncio.Lock()
FLEET_TTL_SECONDS = 1.0


async def temporal() -> Client:
    global _client
    if _client is None:
        api_key = os.environ.get("TEMPORAL_API_KEY") or None
        tls = os.environ.get(
            "TEMPORAL_TLS", "true" if api_key else "false"
        ).strip().lower() == "true"
        logger.info("connecting to %s ns=%s tls=%s", ADDRESS, NAMESPACE, tls)
        _client = await Client.connect(
            ADDRESS, namespace=NAMESPACE, api_key=api_key, tls=tls
        )
    return _client


# Which X-Forwarded-For entry is the caller, counted from the END. Google's front end
# APPENDS, so leading entries are attacker-chosen and only the tail is trustworthy.
# Direct *.run.app appends just the client, so 1. Behind an external load balancer it
# appends `<client>, <LB IP>`, so set 2 — otherwise the whole room shares one bucket.
XFF_HOPS_FROM_END = int(os.environ.get("XFF_HOPS_FROM_END", "1"))

# Only trust the header where a proxy adds it (Cloud Run sets K_SERVICE). Locally it
# is pure client input. Also needs uvicorn --no-proxy-headers, which otherwise
# rewrites request.client from the forged first entry.
_TRUST_XFF = bool(os.environ.get("K_SERVICE"))


def _client_ip(request: Request) -> str:
    """The attendee's IP, for rate-limiting only.

    Never the FIRST X-Forwarded-For entry: that is whatever the caller typed, and
    rotating it defeats the limit entirely. See XFF_HOPS_FROM_END above.
    """
    if _TRUST_XFF:
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",")]
        hops = [h for h in hops if h]
        if len(hops) >= XFF_HOPS_FROM_END:
            return hops[-XFF_HOPS_FROM_END]
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str) -> bool:
    """Per-IP ask limit.

    In-memory, so it is per-instance rather than global — imperfect if the Service
    scales out, and adequate because the ceiling that actually bounds spend is
    `max_instances` on the Worker Pool.

    `_asks` is swept of stale buckets on every call. It used to only prune
    timestamps *within* a bucket and never remove keys, so a request stream with a
    varying client address grew it without limit until the instance was OOM-killed.
    """
    now = time.monotonic()
    for stale in [k for k, v in _asks.items() if not v or now - v[-1] > 60]:
        del _asks[stale]

    recent = [t for t in _asks.get(ip, []) if now - t < 60]
    if len(recent) >= ASKS_PER_MINUTE_PER_IP:
        _asks[ip] = recent
        return True
    recent.append(now)
    _asks[ip] = recent
    return False


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


@app.post("/api/ask")
async def ask(request: Request) -> dict:
    body = await request.json()
    question = (body.get("question") or "").strip()

    if not _passcode_ok(body.get("passcode")):
        raise HTTPException(status_code=403, detail="Wrong passcode.")
    if not question:
        raise HTTPException(status_code=400, detail="Ask something first.")
    if len(question) > MAX_QUESTION_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Keep it under {MAX_QUESTION_CHARS} characters.",
        )

    if _rate_limited(_client_ip(request)):
        raise HTTPException(
            status_code=429, detail="One at a time — give the last one a moment."
        )

    client = await temporal()
    workflow_id = f"research-{uuid.uuid4().hex[:10]}"
    # Started by NAME so this module never imports the research app.
    await client.start_workflow(
        WORKFLOW_NAME, question, id=workflow_id, task_queue=TASK_QUEUE
    )
    logger.info("started %s: %r", workflow_id, question[:80])
    return {"id": workflow_id}


@app.get("/api/run/{workflow_id}")
async def run_state(workflow_id: str) -> JSONResponse:
    """The `progress` Query. The only run-state source the page reads.

    404 means "no such run" and NOTHING ELSE. A Query needs a Worker with a free
    workflow-task slot to answer it, and on this platform there routinely isn't one:
    the pool sits at zero until the WCI reacts, and during fan-out every instance is
    busy with a multi-minute research Activity. Those Queries fail with
    "Timeout expired", which is transient and must read as 503 so the page keeps
    polling. Mapping every exception to 404 made the page declare healthy runs dead
    ~30s in — right in the scale-from-zero window the demo is about.
    """
    client = await temporal()
    handle = client.get_workflow_handle(workflow_id)
    try:
        state = await handle.query("progress", rpc_timeout=timedelta(seconds=10))
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail="No such run.") from exc
        logger.info("query %s unavailable: %s", workflow_id, exc)
        raise HTTPException(status_code=503, detail="Waiting for a worker.") from exc
    except Exception as exc:
        logger.info("query %s failed: %s", workflow_id, exc)
        raise HTTPException(status_code=503, detail="Waiting for a worker.") from exc
    return JSONResponse(state)


@app.post("/api/run/{workflow_id}/decision")
async def decide(workflow_id: str, request: Request) -> dict:
    """Send the review Signal — the event that wakes a pool sitting at zero.

    Guarded exactly like `/api/ask`, because it spends exactly like `/api/ask`: a
    `refine` decision starts a SECOND fan-out. Without these two checks, knowing a run
    id was enough to spend Claude tokens repeatedly, bypassing both the passcode and
    the per-IP limit. Run ids are `research-<uuid4[:10]>` and there is no listing
    endpoint, so they are not enumerable — but they are on screen in front of a room.
    """
    body = await request.json()
    decision = (body.get("decision") or "").strip()

    if not _passcode_ok(body.get("passcode")):
        raise HTTPException(status_code=403, detail="Wrong passcode.")
    if decision not in ("accept", "refine"):
        raise HTTPException(status_code=400, detail="decision must be accept or refine")

    # Only `refine` costs anything, so only `refine` consumes the budget. Rate-limiting
    # `accept` would strand a finished run behind a cooldown for no benefit.
    if decision == "refine" and _rate_limited(_client_ip(request)):
        raise HTTPException(
            status_code=429, detail="One at a time — give the last one a moment."
        )

    note = (body.get("note") or "").strip()[:MAX_QUESTION_CHARS]
    client = await temporal()
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal("review", args=[decision, note])
    logger.info("signalled %s: %s", workflow_id, decision)
    return {"ok": True}


@app.get("/api/fleet")
async def fleet() -> dict:
    """The footer's numbers, led by the Serverless Workers count.

    Cached for `FLEET_TTL_SECONDS`. Read the note on `_fleet_cache`: this is read
    by the whole room, not one projector.
    """
    global _fleet_cache, _fleet_cached_at

    now = time.monotonic()
    if _fleet_cache is not None and now - _fleet_cached_at < FLEET_TTL_SECONDS:
        return _fleet_cache

    async with _fleet_lock:
        # Re-check inside the lock: whoever queued behind the refresh should use
        # its result rather than immediately refreshing again.
        now = time.monotonic()
        if _fleet_cache is not None and now - _fleet_cached_at < FLEET_TTL_SECONDS:
            return _fleet_cache
        _fleet_cache = await _read_fleet()
        _fleet_cached_at = time.monotonic()
        return _fleet_cache


async def _read_fleet() -> dict:
    """The uncached read. Called only from `fleet()` behind the lock.

    The rising-edge derivation for `peak`/`started` lives HERE rather than in the
    request handler, and that placement is load-bearing: if it ran per request
    while the value was cached, every cached hit would compare a fresh `workers`
    against a `_last_workers` that had already been advanced, and `started` would
    drift. Once per refresh is exactly once per genuine observation.
    """
    global _peak_workers, _total_started, _last_workers

    client = await temporal()

    identities: set[str] = set()
    backlog = 0
    ok = False
    for queue_type in (
        TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
        TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
    ):
        try:
            resp = await client.workflow_service.describe_task_queue(
                DescribeTaskQueueRequest(
                    namespace=NAMESPACE,
                    task_queue=TaskQueue(name=TASK_QUEUE),
                    task_queue_type=queue_type,
                    report_stats=True,
                )
            )
        except Exception as exc:
            logger.warning("describe_task_queue(%s) failed: %s", queue_type, exc)
            continue
        ok = True
        # One Worker polls both queue types with the same identity, so the union of
        # identities is the Worker count rather than double it.
        identities.update(p.identity for p in resp.pollers)
        stats = getattr(resp, "stats", None)
        if stats is not None:
            # What the Worker Controller itself reacts to. Only populated when the
            # server supports report_stats; treated as best-effort.
            backlog = max(backlog, getattr(stats, "approximate_backlog_count", 0) or 0)

    # A failed RPC is NOT a scale-to-zero. Treating it as `workers = 0` also poisoned
    # `_last_workers`, so the next good poll saw a false rising edge and permanently
    # inflated `started`. Keep the last good observation.
    if not ok:
        if _fleet_cache is not None:
            return _fleet_cache
        workers = _last_workers
    else:
        workers = len(identities)

        # Rising edges give cumulative starts; no persistence needed.
        if workers > _last_workers:
            _total_started += workers - _last_workers
    _last_workers = workers
    _peak_workers = max(_peak_workers, workers)

    try:
        running = (
            await client.count_workflows(
                f"WorkflowType = '{WORKFLOW_NAME}' AND ExecutionStatus = 'Running'"
            )
        ).count
    except Exception as exc:
        logger.warning("count_workflows failed: %s", exc)
        running = -1

    return {
        "workers": workers,
        "peak": _peak_workers,
        "started": _total_started,
        "workflows_running": running,
        "backlog": backlog,
        "task_queue": TASK_QUEUE,
    }


# No `/api/runs`, deliberately. Run ids are `research-<uuid4 hex>`, so /api/run/{id}
# is capability-based; listing those ids on a public Service would make every
# attendee's question readable by a stranger. `temporal workflow list` does the job.


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


# --------------------------------------------------------------------------
# The page — one HTML file, one stylesheet, self-hosted fonts. No build step.
# --------------------------------------------------------------------------


def _fresh(resp: FileResponse) -> FileResponse:
    """Force revalidation on the files that change between deploys.

    FileResponse sends ETag and Last-Modified but no Cache-Control, and browsers
    then apply *heuristic* freshness — they may serve a stale copy without asking.
    A phone that loaded the page before a redeploy would keep the old UI for the
    rest of the talk. The ETag still makes the revalidation a cheap 304.

    Not applied to the fonts or the logo: those are large, and they only change when
    the brand does.
    """
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
@app.get("/r/{workflow_id}")
async def index(workflow_id: str = "") -> FileResponse:
    """The console. `/r/{id}` is the resumable form.

    The resumable URL is what makes the review pause work at all: the attendee
    closes the tab, goes for coffee, reopens the link, and the draft is still
    there — because it lives in Temporal, not in the page.

    There is no separate presenter page any more. This one carries the fleet
    counters in its footer and scales them with the viewport, so the same URL
    serves a phone in a seat and a projector at the front of the room.
    """
    return _fresh(FileResponse(STATIC / "index.html"))


@app.get("/app.css")
async def stylesheet() -> FileResponse:
    return _fresh(FileResponse(STATIC / "app.css", media_type="text/css"))


@app.get("/temporal-logo.svg")
async def logo() -> FileResponse:
    """The official horizontal lockup, light variant, from temporal.io/brand."""
    return FileResponse(STATIC / "temporal-logo.svg", media_type="image/svg+xml")


# The Learn cards, served as raw markdown for the page to render into a dialog.
#
# `learn/README.md` is the single source of truth — it is a real document people read
# on GitHub, and duplicating it into the page would guarantee the two drift. It lives
# OUTSIDE `web/`, so it needs both this route and its own `COPY` in the Dockerfile;
# shipping it and serving it are separate problems and missing either one produces a
# modal that works locally and 404s on Cloud Run.
LEARN_MD = Path(__file__).parent / "learn" / "README.md"


@app.get("/api/learn")
async def learn() -> FileResponse:
    if not LEARN_MD.exists():
        raise HTTPException(status_code=404, detail="Learn cards are not installed.")
    return _fresh(FileResponse(LEARN_MD, media_type="text/markdown; charset=utf-8"))


# Self-hosted fonts. A mount rather than a route per file, because there are
# several plus their OFL licences. They must be SERVED as well as shipped —
# `COPY web/ ./web/` puts them in the image, but nothing would hand them out
# without this, and the page would silently fall back to system type.
app.mount("/fonts", StaticFiles(directory=STATIC / "fonts"), name="fonts")
