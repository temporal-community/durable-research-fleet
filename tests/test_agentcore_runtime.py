"""The AgentCore session lifecycle, offline.

The properties that fail SILENTLY in production, each pinned here:
  - a session that is never released stays HealthyBusy (billed) until MaxLifetime;
  - a second invocation must not start a second Worker in the same process;
  - a secret that cannot be read must stop the Worker, not degrade into an auth error;
  - the Worker must drain once idle, or sessions never end.
"""

import asyncio
import pathlib

import pytest

import agentcore_runtime as ac
from activities import say_hello
from agentcore_runtime import ActivityTracker, AgentCoreWorker, resolve_secrets
from workflows import HelloWorkflow

from .conftest import TASK_QUEUE, make_current


# --- secrets ----------------------------------------------------------------


def test_secret_is_resolved_from_its_arn(monkeypatch):
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    monkeypatch.setenv("TEMPORAL_API_KEY_SECRET_ARN", "arn:secret:temporal")
    seen = []
    filled = resolve_secrets(fetch=lambda arn: seen.append(arn) or " key-value\n")
    assert filled == ["TEMPORAL_API_KEY"]
    assert seen == ["arn:secret:temporal"]
    assert __import__("os").environ["TEMPORAL_API_KEY"] == "key-value"  # stripped


def test_an_explicit_value_beats_the_secret(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "local")
    monkeypatch.setenv("GEMINI_API_KEY_SECRET_ARN", "arn:secret:gemini")

    def boom(_):
        raise AssertionError("must not fetch when the value is already set")

    assert resolve_secrets(fetch=boom) == []


def test_an_unset_arn_is_skipped(monkeypatch):
    """The hello app needs no model key, so a missing ARN is not an error."""
    for n in ac.SECRET_ENV_NAMES:
        monkeypatch.delenv(n, raising=False)
        monkeypatch.delenv(f"{n}_SECRET_ARN", raising=False)
    assert resolve_secrets(fetch=lambda _: pytest.fail("no ARN, no fetch")) == []


def test_an_empty_temporal_key_fails_loudly(monkeypatch):
    """Without it the Worker cannot authenticate; failing here names the cause."""
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    monkeypatch.setenv("TEMPORAL_API_KEY_SECRET_ARN", "arn:secret:temporal")
    with pytest.raises(RuntimeError, match="empty"):
        resolve_secrets(fetch=lambda _: "  ")


def test_an_unseeded_model_key_does_not_stop_the_worker(monkeypatch, caplog):
    """Both providers share one deployment; one unseeded key must not take down the other
    provider or the hello smoke test. And the log must not carry the value.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY_SECRET_ARN", "arn:secret:anthropic")
    monkeypatch.setenv("GEMINI_API_KEY_SECRET_ARN", "arn:secret:gemini")

    def fetch(arn):
        if "anthropic" in arn:
            raise LookupError("ResourceNotFoundException: no value")
        return "gemini-secret-value"

    assert resolve_secrets(fetch=fetch) == ["GEMINI_API_KEY"]
    assert "ANTHROPIC_API_KEY" not in __import__("os").environ
    assert "gemini-secret-value" not in caplog.text


def test_an_unreadable_secret_fails_loudly(monkeypatch):
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    monkeypatch.setenv("TEMPORAL_API_KEY_SECRET_ARN", "arn:secret:temporal")

    def denied(_):
        raise PermissionError("AccessDenied")

    with pytest.raises(PermissionError):
        resolve_secrets(fetch=denied)


# --- idle tracking -----------------------------------------------------------


async def test_idle_returns_after_the_debounce_with_nothing_running():
    t = ActivityTracker()
    await asyncio.wait_for(t.wait_until_idle(0.05), timeout=2)


async def test_an_inflight_activity_blocks_idle_even_with_no_events():
    """A model call can run for minutes without a single event in the window."""
    t = ActivityTracker()
    t.inflight = 1
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(t.wait_until_idle(0.05), timeout=0.4)
    t.inflight = 0
    await asyncio.wait_for(t.wait_until_idle(0.05), timeout=2)


async def test_an_activity_event_resets_the_window():
    t = ActivityTracker()

    async def poke():
        for _ in range(4):
            await asyncio.sleep(0.05)
            t.changed.set()

    poker = asyncio.create_task(poke())
    start = asyncio.get_running_loop().time()
    await asyncio.wait_for(t.wait_until_idle(0.15), timeout=3)
    assert asyncio.get_running_loop().time() - start >= 0.3  # waited out the pokes
    await poker


# --- session lifecycle -------------------------------------------------------


class _Hooks:
    def __init__(self):
        self.added: list[str] = []
        self.completed: list[int] = []

    def add(self, name: str) -> int:
        self.added.append(name)
        return len(self.added)

    def complete(self, task_id: int) -> None:
        self.completed.append(task_id)


def _worker(hooks: _Hooks) -> AgentCoreWorker:
    return AgentCoreWorker([], [], add_task=hooks.add, complete_task=hooks.complete)


async def test_a_second_invocation_does_not_start_a_second_worker(monkeypatch):
    hooks = _Hooks()
    w = _worker(hooks)
    release = asyncio.Event()

    async def polling():
        await release.wait()

    monkeypatch.setattr(w, "_poll_until_idle", polling)
    first = w.ensure_started()
    await asyncio.sleep(0)
    second = w.ensure_started()
    assert first["message"] == "worker starting"
    assert second["message"] == "worker already polling"
    assert hooks.added == ["temporal-worker"]  # one async task, so one Worker
    release.set()
    await w._task
    assert hooks.completed == [1]


async def test_an_invocation_during_drain_restarts_polling_not_a_false_ack(monkeypatch):
    """Review finding: while the Worker drains nothing polls. Acking 'already polling' there
    consumed the invocation and released the session, stranding the backlog.
    """
    hooks = _Hooks()
    w = _worker(hooks)
    polls = 0
    drain_started = asyncio.Event()
    finish_drain = asyncio.Event()

    async def poll():
        nonlocal polls
        polls += 1
        if polls == 1:
            w._draining = True  # what _poll_until_idle does once idle
            drain_started.set()
            await finish_drain.wait()

    monkeypatch.setattr(w, "_poll_until_idle", poll)
    w.ensure_started()
    await drain_started.wait()
    ack = w.ensure_started()
    assert ack["message"] != "worker already polling"
    assert hooks.added == ["temporal-worker"], "one session task, restarted, not a second one"
    finish_drain.set()
    await w._task
    assert polls == 2, "the draining Worker must poll again after it finishes"
    assert hooks.completed == [1]


async def test_the_session_is_released_even_when_the_worker_crashes(monkeypatch):
    hooks = _Hooks()
    w = _worker(hooks)

    async def crash():
        raise RuntimeError("connect failed")

    monkeypatch.setattr(w, "_poll_until_idle", crash)
    w.ensure_started()
    await w._task  # _run swallows and logs; it must not raise
    assert hooks.completed == [1], "an unreleased task pins the session HealthyBusy"
    assert not w.running


async def test_a_released_session_can_start_again(monkeypatch):
    hooks = _Hooks()
    w = _worker(hooks)

    async def quick():
        return None

    monkeypatch.setattr(w, "_poll_until_idle", quick)
    w.ensure_started()
    await w._task
    assert w.ensure_started()["message"] == "worker starting"
    await w._task
    assert hooks.completed == [1, 2]


async def test_the_real_worker_serves_a_workflow_then_drains(env, settings, monkeypatch, wf_id):
    """End to end against a real server: poll, run, go idle, release the session."""
    monkeypatch.setenv("TEMPORAL_ADDRESS", env.client.service_client.config.target_host)
    monkeypatch.setenv("TEMPORAL_NAMESPACE", env.client.namespace)
    monkeypatch.setenv("AGENTCORE_DEBOUNCE_SECONDS", "6")
    hooks = _Hooks()
    w = AgentCoreWorker(
        [HelloWorkflow], [say_hello], add_task=hooks.add, complete_task=hooks.complete
    )
    w.ensure_started()
    # Let the Worker register its Version before routing to it (see conftest.make_current).
    for _ in range(50):
        await asyncio.sleep(0.1)
        try:
            await make_current(env.client, settings.deployment_name, settings.build_id)
            break
        except Exception:
            continue

    result = await env.client.execute_workflow(
        HelloWorkflow.run, "agentcore", id=wf_id, task_queue=TASK_QUEUE
    )
    assert result == "Hello, agentcore!"

    # The window is longer than registration plus one Workflow, or the Worker would
    # (correctly) drain before there was any work — which a real invocation re-triggers.
    await asyncio.wait_for(w._task, timeout=30)  # then idle for the window: must drain
    assert hooks.completed == [1]


# --- the packaging contract --------------------------------------------------


def test_agentcore_runtime_does_not_import_the_sdk():
    """So it stays testable offline; the SDK is bound only in worker_agentcore.py."""
    src = pathlib.Path("agentcore_runtime.py").read_text()
    assert "bedrock_agentcore" not in src.replace("`bedrock_agentcore`", "")
