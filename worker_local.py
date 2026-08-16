"""
Local dev Worker — NOT deployed anywhere. Same runtime as the Cloud Run Worker,
just a different default build id, so what you test locally is what ships.

This DOES need versioning config even locally: `HelloWorkflow` declares
`versioning_behavior=PINNED`, and the server rejects a Workflow Task whose
Workflow declares a versioning behavior while its Worker isn't in versioned mode
("versioning behavior cannot be specified without deployment options being set
with versioned mode"). runtime.py always configures it.

Registers BOTH apps — the hello smoke test and the research agent — exactly as
worker_cloudrun.py does, so Phase 0 exercises the same registration.

Run:
    temporal server start-dev          # in one terminal

    # one-time per dev server: a versioned Task Queue needs a current version
    # to dispatch to, or Workflows sit unassigned forever
    temporal worker deployment set-current-version \
        --deployment-name research-fleet --build-id local --yes

    export GEMINI_API_KEY=...          # only the research app needs this
    python worker_local.py             # in another
    make web-local                     # in a third, then open http://localhost:8000
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
            default_build_id="local",
        )
    )
