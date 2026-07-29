"""Workflow tests against a real (time-skipped) server."""

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError

import runtime
import workflows
from workflows import HelloWorkflow

from .conftest import TASK_QUEUE, make_current


async def test_happy_path(env, worker, wf_id):
    result = await env.client.execute_workflow(
        HelloWorkflow.run, "world", id=wf_id, task_queue=TASK_QUEUE
    )
    assert result == "Hello, world!"


async def test_activity_timeouts_land_in_history(env, worker, wf_id):
    """The serverless-critical settings must actually reach the server, not just
    exist in source. Asserting on history catches a refactor that drops them.
    """
    await env.client.execute_workflow(
        HelloWorkflow.run, "world", id=wf_id, task_queue=TASK_QUEUE
    )

    scheduled = [
        e.activity_task_scheduled_event_attributes
        async for e in env.client.get_workflow_handle(wf_id).fetch_history_events()
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    assert len(scheduled) == 1
    attrs = scheduled[0]

    # Without a heartbeat timeout, a Worker killed by scale-in is undetectable
    # until start_to_close expires (KB §7.3).
    assert attrs.heartbeat_timeout.seconds == workflows.ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS
    assert attrs.start_to_close_timeout.seconds == workflows.ACTIVITY_START_TO_CLOSE_SECONDS
    assert attrs.retry_policy.maximum_attempts == 5
    # A heartbeat timeout that isn't shorter than start_to_close buys nothing.
    assert attrs.heartbeat_timeout.seconds < attrs.start_to_close_timeout.seconds


async def test_retries_then_succeeds(env, settings, wf_id):
    """A scale-in interruption surfaces to the Workflow as a failed Activity.
    Verify the RetryPolicy actually recovers instead of failing the Workflow.
    """
    attempts = 0

    @activity.defn(name="say_hello")
    async def flaky(name: str) -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ApplicationError("simulated interruption")
        return f"Hello, {name}!"

    w = runtime.build_worker(env.client, [HelloWorkflow], [flaky], settings)
    async with w:
        await make_current(env.client, settings.deployment_name, settings.build_id)
        result = await env.client.execute_workflow(
            HelloWorkflow.run, "world", id=wf_id, task_queue=TASK_QUEUE
        )

    assert result == "Hello, world!"
    assert attempts == 3


async def test_gives_up_after_max_attempts(env, settings, wf_id):
    """Retries must be bounded — an Activity that always fails has to surface as a
    Workflow failure rather than retrying forever and pinning a pool instance.
    """

    @activity.defn(name="say_hello")
    async def always_fails(name: str) -> str:
        raise ApplicationError("permanent")

    w = runtime.build_worker(env.client, [HelloWorkflow], [always_fails], settings)
    async with w:
        await make_current(env.client, settings.deployment_name, settings.build_id)
        with pytest.raises(WorkflowFailureError):
            await env.client.execute_workflow(
                HelloWorkflow.run, "world", id=wf_id, task_queue=TASK_QUEUE
            )


async def test_argument_is_passed_through(env, worker, wf_id):
    result = await env.client.execute_workflow(
        HelloWorkflow.run, "Shubham", id=wf_id, task_queue=TASK_QUEUE
    )
    assert result == "Hello, Shubham!"
