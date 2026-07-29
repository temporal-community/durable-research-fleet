"""Activity-level tests, run without a server via ActivityEnvironment."""

import asyncio

import pytest
from temporalio.testing import ActivityEnvironment

import activities
from activities import say_hello


async def test_returns_greeting():
    env = ActivityEnvironment()
    assert await env.run(say_hello, "world") == "Hello, world!"


async def test_heartbeats_while_running(monkeypatch):
    """KB §7.3: Cloud Run scale-in can stop an instance mid-Activity, so the
    Activity must heartbeat or the server cannot tell a dead Worker from a busy
    one until start_to_close_timeout expires.
    """
    monkeypatch.setattr(activities, "GREETING_SECONDS", 3)
    monkeypatch.setattr(activities, "HEARTBEAT_INTERVAL_SECONDS", 1)

    beats = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *args: beats.append(args)

    await env.run(say_hello, "world")

    # ~1 per second. Assert a floor rather than an exact count so the test isn't
    # brittle under load, but enough to prove it heartbeats more than once.
    assert len(beats) >= 2, f"expected repeated heartbeats, got {beats}"


def test_heartbeat_interval_stays_under_workflow_timeout():
    """The heartbeat interval must be comfortably below the heartbeat_timeout the
    Workflow sets, or a healthy Activity gets killed for being 'late'.
    """
    from workflows import ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS

    assert activities.HEARTBEAT_INTERVAL_SECONDS < ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS


async def test_cancellation_is_observed(monkeypatch):
    """Graceful shutdown cancels in-flight Activities; the sleep must be the
    cancellation point rather than the Activity running to completion regardless.
    """
    monkeypatch.setattr(activities, "GREETING_SECONDS", 30)

    env = ActivityEnvironment()
    task = asyncio.create_task(env.run(say_hello, "world"))
    await asyncio.sleep(0.2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
