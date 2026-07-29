"""The hello app's Activity — the infrastructure smoke test.

Needs no Claude key, which is what keeps `make verify SCALE=1` usable for
diagnosing scaling without spending tokens.
"""

import asyncio

from temporalio import activity

# LOAD-BEARING. Serverless scaling is driven by Task Queue backlog, so the Activity
# must take long enough that a burst actually queues instead of being absorbed
# instantly. Don't "optimise" this away.
GREETING_SECONDS = 5

# Must stay under the Workflow's heartbeat_timeout (workflows.py).
HEARTBEAT_INTERVAL_SECONDS = 1


@activity.defn
async def say_hello(name: str) -> str:
    activity.logger.info("saying hello to %s...", name)

    elapsed = 0.0
    while elapsed < GREETING_SECONDS:
        # Also the cancellation checkpoint: on graceful shutdown the SDK cancels the
        # Activity and CancelledError surfaces from this sleep.
        step = min(HEARTBEAT_INTERVAL_SECONDS, GREETING_SECONDS - elapsed)
        await asyncio.sleep(step)
        elapsed += step
        activity.heartbeat(elapsed)

    return f"Hello, {name}!"
