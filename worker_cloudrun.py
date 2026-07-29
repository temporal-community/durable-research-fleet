"""
The Worker that runs inside the Cloud Run Worker Pool.

Deliberately an ORDINARY long-running Temporal Worker — connect once, register
Workflows/Activities, poll forever. Unlike an AWS Lambda Serverless Worker, a
Cloud Run Serverless Worker needs no per-invocation adapter: the Worker
Controller Instance scales the *number of these long-lived instances* (0 -> N);
it does not invoke this process per task.

All the wiring lives in runtime.py. This file is only the app binding.

TWO apps are registered on the one Task Queue:

  HelloWorkflow     the infrastructure smoke test. No Claude key needed, which is
                    why `make verify SCALE=1` still works on a stack that has no
                    ANTHROPIC_API_KEY configured.
  ResearchWorkflow  the real application.

Registering a second app is also the proof that runtime.py is genuinely
app-agnostic: nothing in it changed to make this work.

Configuration is entirely environmental; see runtime.Settings and
terraform/workerpool.tf, which is what actually sets these in the pool:
  TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, TEMPORAL_TASK_QUEUE,
  TEMPORAL_DEPLOYMENT_NAME, BUILD_ID, TEMPORAL_TLS, TEMPORAL_API_KEY,
  MAX_CONCURRENT_ACTIVITIES, GRACEFUL_SHUTDOWN_SECONDS, ANTHROPIC_API_KEY
"""

import asyncio
import logging

from activities import say_hello
from research_activities import RESEARCH_ACTIVITIES
from research_workflow import ResearchWorkflow
from runtime import run_worker
from workflows import HelloWorkflow

logging.basicConfig(level=logging.INFO)


if __name__ == "__main__":
    asyncio.run(
        run_worker(
            [HelloWorkflow, ResearchWorkflow],
            [say_hello, *RESEARCH_ACTIVITIES],
            default_build_id="v1",
        )
    )
