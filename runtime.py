"""
Reusable Serverless Worker runtime — the infra half of this repo.

Everything here is app-agnostic. To run a different app (e.g. a research agent),
pass different Workflows and Activities to `run_worker`; nothing in this file
should need to change:

    from runtime import run_worker
    asyncio.run(run_worker([MyWorkflow], [my_activity]))

All configuration comes from the environment so the same image can run locally,
on a Cloud Run Worker Pool, or against Temporal Cloud.
"""

import asyncio
import logging
import os
import signal
import socket
import uuid
from dataclasses import dataclass
from datetime import timedelta

from temporalio.client import Client
from temporalio.common import WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

logger = logging.getLogger("serverless-worker")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


@dataclass(frozen=True)
class Settings:
    address: str
    namespace: str
    task_queue: str
    deployment_name: str
    build_id: str
    api_key: str | None
    tls: bool
    max_concurrent_activities: int
    graceful_shutdown_seconds: int

    @classmethod
    def from_env(cls, *, default_build_id: str = "v1") -> "Settings":
        api_key = os.environ.get("TEMPORAL_API_KEY") or None
        # Temporal Cloud requires TLS and an API key; a self-hosted frontend is
        # plaintext gRPC. Keying off the API key makes one image work in both
        # places. TEMPORAL_TLS is the explicit override.
        tls_default = "true" if api_key else "false"
        tls = os.environ.get("TEMPORAL_TLS", tls_default).strip().lower() == "true"

        return cls(
            address=os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
            namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
            task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", "research-queue"),
            # Must match the Worker Deployment the compute config is attached to.
            # Terraform passes this in; a mismatch means Workflow Tasks are never
            # dispatched and Workflows hang with no error.
            deployment_name=os.environ.get("TEMPORAL_DEPLOYMENT_NAME", "research-fleet"),
            build_id=os.environ.get("BUILD_ID", default_build_id),
            api_key=api_key,
            tls=tls,
            # Default 1, deliberately. Two reasons:
            #  - KNOWLEDGE_BASE.md §4: one Activity OOMing takes down every other
            #    Activity sharing the invocation, so slot=1 gives isolation.
            #  - The SDK default of 100 lets a single instance swallow an entire
            #    burst, so no Task Queue backlog forms and the WCI never scales.
            max_concurrent_activities=_env_int("MAX_CONCURRENT_ACTIVITIES", 1),
            # Cloud Run's termination grace period is short; keep under it so we
            # exit cleanly rather than being SIGKILLed mid-drain.
            graceful_shutdown_seconds=_env_int("GRACEFUL_SHUTDOWN_SECONDS", 20),
        )


def worker_identity() -> str:
    """A per-PROCESS Temporal identity, because the SDK default collides on Cloud Run.

    The default is `{pid}@{hostname}`. In a Cloud Run container the worker is PID 1
    and the hostname is `localhost`, so EVERY instance reports `1@localhost`. The
    Serverless Workers counter counts DISTINCT POLLER IDENTITIES, so it read 1 no
    matter how many instances were running — the demo's headline number, wrong on
    stage but correct locally (real pids and hostnames), which is exactly why it
    survived every local test.

    Measured on a live run 2026-07-29: six sub-questions all started in the same
    second and overlapped, so six instances ran them, yet all eight Activities in
    history reported `1@localhost`.

    The random suffix is what guarantees uniqueness; pid@host stays in front because
    it is still the useful part to read in a log.
    """
    return f"{os.getpid()}@{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


async def connect(settings: Settings) -> Client:
    identity = worker_identity()
    logger.info(
        "connecting to %s ns=%s tls=%s api_key=%s identity=%s",
        settings.address,
        settings.namespace,
        settings.tls,
        "set" if settings.api_key else "unset",
        identity,
    )
    return await Client.connect(
        settings.address,
        namespace=settings.namespace,
        api_key=settings.api_key,
        tls=settings.tls,
        identity=identity,
    )


def build_worker(client: Client, workflows: list, activities: list, settings: Settings) -> Worker:
    """Construct the Worker.

    Split out from run_worker so tests can build the EXACT same Worker instead of
    hand-copying its configuration. That copying is not a hypothetical risk: a
    test fixture that mirrored this by hand and omitted `use_worker_versioning`
    made every Workflow Task fail with "versioning behavior cannot be specified
    without deployment options being set with versioned mode".
    """
    return Worker(
        client,
        task_queue=settings.task_queue,
        workflows=workflows,
        activities=activities,
        max_concurrent_activities=settings.max_concurrent_activities,
        # Without this the pool-level scale-in that Cloud Run performs (see
        # KNOWLEDGE_BASE.md §7.3 — it does not know which instance is busy)
        # kills in-flight Activities with no chance to finish or report.
        graceful_shutdown_timeout=timedelta(seconds=settings.graceful_shutdown_seconds),
        # Serverless Workers require Worker Versioning. A Workflow that declares
        # a versioning behavior is rejected outright if its Worker is unversioned.
        deployment_config=WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(
                deployment_name=settings.deployment_name,
                build_id=settings.build_id,
            ),
            use_worker_versioning=True,
        ),
    )


async def run_worker(workflows: list, activities: list, *, default_build_id: str = "v1") -> None:
    """Run a versioned Worker until SIGTERM/SIGINT, then drain gracefully."""
    settings = Settings.from_env(default_build_id=default_build_id)
    client = await connect(settings)
    worker = build_worker(client, workflows, activities, settings)

    # temporalio installs no signal handlers and asyncio does not turn SIGTERM
    # into anything catchable, so without this the process dies instantly on
    # scale-in and the Activity Task is silently abandoned.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # e.g. Windows
            logger.warning("no signal handler for %s on this platform", sig)

    logger.info(
        "polling '%s' as %s/%s (activity slots=%d, grace=%ds)",
        settings.task_queue,
        settings.deployment_name,
        settings.build_id,
        settings.max_concurrent_activities,
        settings.graceful_shutdown_seconds,
    )

    # Exiting the context manager begins a graceful shutdown: stop polling,
    # cancel in-flight Activities, wait up to graceful_shutdown_timeout.
    async with worker:
        await stop.wait()

    logger.info("shutdown signal received; drained and exiting")
