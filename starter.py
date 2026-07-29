"""
Starts HelloWorkflow executions.

Each one is a Workflow Execution picked up by a Worker. When the Worker is a
Cloud Run Worker Pool instance, `--count N` is the interesting part: it queues N
Workflows at once, which is what makes Temporal's Worker Controller scale the
pool up — and then back down once the queue goes quiet.

Usage:
    python starter.py                      # greet "world"
    python starter.py Shubham --watch      # wait for the result
    python starter.py world --count 20     # burst: 20 at once, watch the pool scale

Env (all optional locally — defaults target `temporal server start-dev`):
    TEMPORAL_ADDRESS    default localhost:7233
    TEMPORAL_NAMESPACE  default default
    TEMPORAL_TASK_QUEUE default research-queue
    TEMPORAL_API_KEY    Temporal Cloud only; setting it turns TLS on
    TEMPORAL_TLS        explicit true/false override
"""

import argparse
import asyncio
import os
import time

from temporalio.client import Client


async def connect() -> Client:
    # Temporal Cloud needs TLS; a self-hosted/dev frontend is plaintext. Key off
    # the API key (only Cloud needs one) so this works in both places, with
    # TEMPORAL_TLS as an explicit override.
    api_key = os.environ.get("TEMPORAL_API_KEY")
    tls = os.environ.get("TEMPORAL_TLS", "true" if api_key else "false").lower() == "true"

    return await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        api_key=api_key,
        tls=tls,
    )


async def start_one(client: Client, name: str, task_queue: str, watch: bool, index: int) -> None:
    workflow_id = f"hello-{int(time.time() * 1000)}-{index}"
    handle = await client.start_workflow(
        "HelloWorkflow",
        name,
        id=workflow_id,
        task_queue=task_queue,
    )
    print(f"→ started  {workflow_id}")

    if watch:
        result = await handle.result()
        print(f"✅ {result}  ({workflow_id})")


async def run(name: str, watch: bool, count: int) -> None:
    client = await connect()
    task_queue = os.environ.get("TEMPORAL_TASK_QUEUE", "research-queue")

    if count > 1:
        print(f"burst: starting {count} Workflows at once on '{task_queue}'...\n")

    await asyncio.gather(
        *[start_one(client, name, task_queue, watch, i) for i in range(count)]
    )

    if count > 1 and not watch:
        print("\nQueued. Watch the pool scale:\n  make status")


def main() -> None:
    parser = argparse.ArgumentParser(description="Start HelloWorkflow executions")
    parser.add_argument("name", nargs="?", default="world", help="who to greet (default: world)")
    parser.add_argument("--watch", action="store_true", help="wait for and print each result")
    parser.add_argument(
        "--count", type=int, default=1, help="start N Workflows at once (burst mode)"
    )
    args = parser.parse_args()

    asyncio.run(run(args.name, args.watch, args.count))


if __name__ == "__main__":
    main()
