"""The hello app's Workflow. One execution == one greeting."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, VersioningBehavior

with workflow.unsafe.imports_passed_through():
    from activities import say_hello

# Invariant: HEARTBEAT_INTERVAL (activities.py) < HEARTBEAT_TIMEOUT < START_TO_CLOSE.
ACTIVITY_START_TO_CLOSE_SECONDS = 30
ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS = 10


# `versioning_behavior` is REQUIRED, not boilerplate: Serverless Workers require
# Worker Versioning, and a Workflow that declares one needs a Worker in versioned
# mode or the server rejects every Workflow Task ("versioning behavior cannot be
# specified without deployment options being set with versioned mode").
# PINNED keeps an in-flight execution on the Version it started on.
@workflow.defn(versioning_behavior=VersioningBehavior.PINNED)
class HelloWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            say_hello,
            name,
            start_to_close_timeout=timedelta(seconds=ACTIVITY_START_TO_CLOSE_SECONDS),
            # The important one on Cloud Run: pool-level scale-in can stop an
            # instance mid-Activity, and without this the server waits out the full
            # start_to_close before retrying instead of ~10s.
            heartbeat_timeout=timedelta(seconds=ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=10),
                maximum_attempts=5,
            ),
        )
