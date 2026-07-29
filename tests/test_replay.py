"""Replay / determinism tests.

Worth having on this project specifically: `HelloWorkflow` is PINNED, so an
in-flight execution finishes on the Version it started on. The moment anyone
switches to AUTO_UPGRADE, or edits the Workflow while executions are open, replay
safety stops being theoretical. This catches non-determinism before deploy.
"""

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from workflows import HelloWorkflow

from .conftest import TASK_QUEUE


async def test_history_replays_deterministically(env, worker, wf_id):
    await env.client.execute_workflow(
        HelloWorkflow.run, "world", id=wf_id, task_queue=TASK_QUEUE
    )
    history = await env.client.get_workflow_handle(wf_id).fetch_history()

    # Raises on any non-determinism between the recorded history and current code.
    await Replayer(workflows=[HelloWorkflow]).replay_workflow(history)


async def test_replayer_rejects_a_mismatched_history(env, worker, wf_id):
    """Prove the replay check has teeth — a history from a different Workflow
    shape must fail rather than pass silently.
    """
    await env.client.execute_workflow(
        HelloWorkflow.run, "world", id=wf_id, task_queue=TASK_QUEUE
    )
    history = await env.client.get_workflow_handle(wf_id).fetch_history()

    # Rewrite the recorded Activity name so replay cannot line up.
    as_dict = history.to_json_dict()
    for event in as_dict["events"]:
        attrs = event.get("activityTaskScheduledEventAttributes")
        if attrs:
            attrs["activityType"]["name"] = "some_other_activity"

    with pytest.raises(Exception):
        await Replayer(workflows=[HelloWorkflow]).replay_workflow(
            WorkflowHistory.from_json(wf_id, as_dict)
        )
