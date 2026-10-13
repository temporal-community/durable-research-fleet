"""
The AgentCore half of the Serverless Worker runtime.

`runtime.py` is the infra half for a long-lived Worker (Cloud Run, local). This file
is its sibling for Amazon Bedrock AgentCore Runtime, and like `runtime.py` it is
app-agnostic: the app binding is `worker_agentcore.py`.

HOW AGENTCORE DIFFERS FROM CLOUD RUN
  Cloud Run  Temporal scales the NUMBER of long-lived instances; each polls forever.
  AgentCore  Temporal INVOKES a Runtime endpoint. That starts (or reuses) a session
             — a microVM that can live up to 8 hours — and the Worker must start
             itself, poll while there is work, and let the session end when there is
             none. Nothing outside the session decides that; the Worker does.

THE CONTRACT WITH THE SESSION
  /invocations must return fast. If it held the response open for the polling window
  the invocation would time out. So the handler registers an async task, which makes
  /ping report "HealthyBusy" (AgentCore keeps the session alive while that is true),
  starts the Worker in the background, and returns. When the Worker has been idle for
  `AGENTCORE_DEBOUNCE_SECONDS` it drains, the task is completed, /ping goes back to
  "Healthy", and AgentCore reclaims the session after its idle timeout. Forgetting to
  complete the task pins the session HealthyBusy until MaxLifetime and is billed.

WHAT THIS FILE DELIBERATELY DOES NOT DO
  - Import `bedrock_agentcore`. The HTTP app is injected as two callables, so this
    module and its tests need neither the SDK nor a network.
  - Install signal handlers. The Worker lives inside the AgentCore app's event loop;
    on termination that loop is cancelled and the `async with worker` exit performs
    the graceful shutdown. Handlers here would fight the server's.
  - Change the slot limit. `runtime.Settings` still defaults it to 1, so one question
    still fans out to one session per sub-question where the scaler allows it.
"""

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

import runtime

logger = logging.getLogger("agentcore-runtime")

# Secrets are referenced, never inlined. A Runtime's environment variables are visible
# to anyone who can read the Runtime configuration, so the Terraform stack passes
# `<NAME>_SECRET_ARN` and the session resolves the value itself with its execution
# role. Each name here has exactly one purpose, so the role's policy can be exact.
#
# Only the Temporal key is REQUIRED. Provider keys are optional because the deployment
# serves both providers and a stack seeded for one must still run: a Worker that
# refused to start for want of the other provider's key would take the hello smoke
# test and the working provider down with it. The research Activity for an unseeded
# provider fails with llm.py's own "key not set" error, which names the cause.
REQUIRED_SECRETS = ("TEMPORAL_API_KEY",)
OPTIONAL_SECRETS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY")
SECRET_ENV_NAMES = REQUIRED_SECRETS + OPTIONAL_SECRETS
_OPTIONAL_LABELS = {"GEMINI_API_KEY": "Gemini", "ANTHROPIC_API_KEY": "Anthropic"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    return value if value >= 0 else default


def resolve_secrets(
    *,
    names: tuple[str, ...] = SECRET_ENV_NAMES,
    fetch: Callable[[str], str] | None = None,
) -> list[str]:
    """Fill `NAME` from the secret named by `NAME_SECRET_ARN`; return what was filled.

    An explicit `NAME` wins, so local runs and tests need no AWS. A REQUIRED secret that
    cannot be read, or is empty, RAISES: starting without the Temporal key would only
    surface later as an authentication failure that looks like a network problem.
    An OPTIONAL one is logged and skipped (see OPTIONAL_SECRETS).

    An unset ARN is simply skipped.
    """
    filled: list[str] = []
    for name in names:
        arn = os.environ.get(f"{name}_SECRET_ARN")
        if not arn or os.environ.get(name):
            continue
        optional = name in OPTIONAL_SECRETS
        try:
            if fetch is None:
                fetch = _secrets_manager_fetch()
            value = fetch(arn).strip()
            if not value:
                raise RuntimeError(f"secret {arn} for {name} is empty; seed a value")
        except Exception as exc:
            if not optional:
                raise
            # Log a fixed provider label and the exception CLASS only: no secret-bearing
            # name or message text reaches the log (the value is never in `exc`, but a
            # scanner cannot know that, and the class is enough to diagnose).
            logger.warning(
                "optional %s secret unavailable (%s); continuing without it",
                _OPTIONAL_LABELS[name],
                type(exc).__name__,
            )
            continue
        os.environ[name] = value
        filled.append(name)
    return filled


def _secrets_manager_fetch() -> Callable[[str], str]:
    import boto3  # arrives with bedrock-agentcore; imported late so tests never need it

    client = boto3.client("secretsmanager")

    def fetch(arn: str) -> str:
        return client.get_secret_value(SecretId=arn)["SecretString"]

    return fetch


class ActivityTracker(Interceptor):
    """Counts in-flight Activities so a session only ends when the Worker is truly idle.

    "Idle" means nothing STARTED OR FINISHED for the debounce window AND nothing is
    running. Both halves matter: a model call can run for minutes with no events in
    the window, and a burst of short Activities can leave gaps shorter than it.

    Workflow Tasks are not tracked. They are brief and always follow an Activity event,
    which resets the window; and if one lands just after the Worker drains, Temporal
    simply invokes the Runtime again.
    """

    def __init__(self) -> None:
        self.inflight = 0
        self.changed = asyncio.Event()

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _TrackedActivity(next, self)

    async def wait_until_idle(self, debounce: float) -> None:
        """Return once no Activity has run, and none is running, for `debounce` seconds."""
        while True:
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=debounce)
            except asyncio.TimeoutError:
                if self.inflight == 0:
                    return


class _TrackedActivity(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, tracker: ActivityTracker) -> None:
        super().__init__(next)
        self._tracker = tracker

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        self._tracker.inflight += 1
        self._tracker.changed.set()
        try:
            return await self.next.execute_activity(input)
        finally:
            self._tracker.inflight -= 1
            self._tracker.changed.set()


class AgentCoreWorker:
    """Owns one Worker for the life of one Runtime session.

    `add_task` / `complete_task` are the AgentCore app's async-task hooks. Injected so
    that the lifecycle — start once, always release — is testable without the SDK.
    """

    def __init__(
        self,
        workflows: list,
        activities: list,
        *,
        add_task: Callable[[str], int],
        complete_task: Callable[[int], Any],
        default_build_id: str = "v1",
    ) -> None:
        self._workflows = workflows
        self._activities = activities
        self._add_task = add_task
        self._complete_task = complete_task
        self._default_build_id = default_build_id
        # asyncio keeps only a weak reference to a Task, so this is also what stops the
        # Worker being garbage-collected mid-poll. It doubles as "already running?".
        self._task: asyncio.Task[None] | None = None
        # True from the moment the Worker decides it is idle until it has finished
        # draining. In that window the Worker is NOT polling, so an invocation that
        # lands now must not be acknowledged as "already polling" (see ensure_started).
        self._draining = False
        self._restart = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def ensure_started(self) -> dict:
        """Start the Worker unless this session already has one. Never blocks.

        A second invocation can arrive while the first Worker is still polling (the
        scaler asked for more capacity than this session already provides). Starting
        a second Worker in the same process would double the pollers behind one slot
        limit and misreport the fleet, so it is acknowledged and ignored.
        """
        queue = os.environ.get("TEMPORAL_TASK_QUEUE", "research-queue")
        if self.running and self._draining:
            # Temporal invoked us because work arrived, and we are mid-shutdown: nothing is
            # polling any more. Acknowledging "already polling" would consume the
            # invocation while the Worker finishes draining and the session is released,
            # stranding the backlog until the scaler invokes again. Ask the running task
            # to poll again as soon as the drain completes instead.
            self._restart = True
            return {"message": "worker draining; will resume polling", "task_queue": queue}
        if self.running:
            return {"message": "worker already polling", "task_queue": queue}
        task_id = self._add_task("temporal-worker")
        self._task = asyncio.create_task(self._run(task_id))
        return {"message": "worker starting", "task_queue": queue}

    async def _run(self, task_id: int) -> None:
        try:
            while True:
                self._draining = False
                await self._poll_until_idle()
                if not self._restart:
                    break
                self._restart = False
                logger.info("an invocation arrived while draining; polling again")
        except Exception:
            # Nothing awaits this Task, so an unlogged error would vanish. The session
            # is then released and Temporal will invoke a fresh one if work remains.
            logger.exception("worker failed; releasing the session")
        finally:
            # Without this the session stays HealthyBusy until MaxLifetime.
            self._complete_task(task_id)

    async def _poll_until_idle(self) -> None:
        loop = asyncio.get_running_loop()
        # boto3 is synchronous; keep it off the event loop that /ping is served from.
        filled = await loop.run_in_executor(None, resolve_secrets)
        if filled:
            logger.info("resolved from Secrets Manager: %s", ", ".join(filled))

        settings = runtime.Settings.from_env(default_build_id=self._default_build_id)
        debounce = _env_float("AGENTCORE_DEBOUNCE_SECONDS", 60.0)
        if debounce <= 0:  # wait_for(timeout=0) would drain before serving anything
            debounce = 60.0
        client = await runtime.connect(settings)
        tracker = ActivityTracker()
        worker = runtime.build_worker(
            client, self._workflows, self._activities, settings, interceptors=[tracker]
        )
        logger.info(
            "polling '%s' as %s/%s (activity slots=%d, idle=%.0fs, grace=%ds)",
            settings.task_queue,
            settings.deployment_name,
            settings.build_id,
            settings.max_concurrent_activities,
            debounce,
            settings.graceful_shutdown_seconds,
        )
        async with worker:
            await tracker.wait_until_idle(debounce)
            # No await between the idle decision and this flag, so ensure_started can
            # never observe "running and not draining" once the Worker has stopped polling.
            self._draining = True
        logger.info("idle for %.0fs; drained and releasing the session", debounce)
