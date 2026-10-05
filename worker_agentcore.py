"""
The Worker that runs inside an Amazon Bedrock AgentCore Runtime session.

Temporal Cloud's Worker Controller Instance invokes this Runtime's endpoint when a Task
Queue has a backlog. `BedrockAgentCoreApp` supplies the HTTP contract (/ping and
/invocations); `agentcore_runtime.AgentCoreWorker` starts a normal versioned Worker in
the background and lets the session end when the work drains. All the wiring lives
there. This file is only the app binding, exactly as `worker_cloudrun.py` is for Cloud
Run, and it registers the same apps (apps.py) on the one Task Queue.

Configuration is environmental and set by agentcore/main.tf:
  TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, TEMPORAL_TASK_QUEUE, TEMPORAL_DEPLOYMENT_NAME,
  BUILD_ID, MAX_CONCURRENT_ACTIVITIES, GRACEFUL_SHUTDOWN_SECONDS,
  AGENTCORE_DEBOUNCE_SECONDS, and <NAME>_SECRET_ARN for TEMPORAL_API_KEY,
  GEMINI_API_KEY, ANTHROPIC_API_KEY (see agentcore_runtime.resolve_secrets).
"""

import logging

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from agentcore_runtime import AgentCoreWorker
from apps import ACTIVITIES, WORKFLOWS

logging.basicConfig(level=logging.INFO)

app = BedrockAgentCoreApp()

worker = AgentCoreWorker(
    WORKFLOWS,
    ACTIVITIES,
    add_task=app.add_async_task,
    complete_task=app.complete_async_task,
    default_build_id="v1",
)


@app.entrypoint
async def invoke(payload: dict) -> dict:
    """Start the Worker and acknowledge. The payload is unused."""
    return worker.ensure_started()


if __name__ == "__main__":
    app.run()
