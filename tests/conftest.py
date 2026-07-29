"""Shared fixtures.

Three choices, each learned the hard way while writing these tests:

1. `start_local()`, not `start_time_skipping()`. Time-skipping fast-forwards the
   server clock, which buys nothing here — the Activity's cost is a real
   `asyncio.sleep`, and time-skipping does not skip Activity execution. Worse, the
   skipped clock races past `heartbeat_timeout` while the Activity is still
   legitimately working, so Activities time out spuriously and the Workflow
   retries until it hangs. A real local server behaves like production.

2. Fixture and test loop scopes both pinned to `module` (see pytest.ini). Mixing a
   module-scoped server fixture with function-scoped test loops makes the client
   await on a loop that never runs it, and the test hangs with NO output.

3. The Worker comes from `runtime.build_worker`, never hand-copied. A fixture that
   mirrored production by hand and dropped `use_worker_versioning` reproduced the
   project's own versioning bug — every Workflow Task rejected, test hung.
"""

import uuid

import pytest
import pytest_asyncio
from temporalio.api.workflowservice.v1 import (
    CreateWorkerDeploymentRequest,
    DescribeWorkerDeploymentRequest,
    SetWorkerDeploymentCurrentVersionRequest,
)
from temporalio.testing import WorkflowEnvironment

import activities
import runtime
from activities import say_hello
from workflows import HelloWorkflow

TASK_QUEUE = "test-queue"
DEPLOYMENT_NAME = "test-deployment"
BUILD_ID = "test-build"

# Keep the suite quick. The 5s production value exists to create Task Queue
# backlog for the Worker Controller; in-process that is irrelevant.
TEST_GREETING_SECONDS = 1


@pytest.fixture(scope="module", autouse=True)
def fast_activity():
    original = activities.GREETING_SECONDS
    activities.GREETING_SECONDS = TEST_GREETING_SECONDS
    yield
    activities.GREETING_SECONDS = original


@pytest.fixture
def settings(monkeypatch) -> runtime.Settings:
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", TASK_QUEUE)
    monkeypatch.setenv("TEMPORAL_DEPLOYMENT_NAME", DEPLOYMENT_NAME)
    monkeypatch.setenv("BUILD_ID", BUILD_ID)
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    monkeypatch.delenv("TEMPORAL_TLS", raising=False)
    return runtime.Settings.from_env()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env():
    async with await WorkflowEnvironment.start_local() as e:
        yield e


async def make_current(client, deployment_name: str, build_id: str) -> None:
    """Point the Task Queue at a Version so Workflows actually dispatch.

    Versioned Task Queues need a *current* version; without this, Workflows sit
    unassigned forever. This is the API equivalent of the one-time
    `temporal worker deployment set-current-version` the README calls for.
    """
    svc = client.workflow_service
    try:
        await svc.create_worker_deployment(
            CreateWorkerDeploymentRequest(
                namespace=client.namespace, deployment_name=deployment_name, identity="tests"
            )
        )
    except Exception:
        pass  # already exists — the steady state on repeat runs

    described = await svc.describe_worker_deployment(
        DescribeWorkerDeploymentRequest(
            namespace=client.namespace, deployment_name=deployment_name
        )
    )
    await svc.set_worker_deployment_current_version(
        SetWorkerDeploymentCurrentVersionRequest(
            namespace=client.namespace,
            deployment_name=deployment_name,
            build_id=build_id,
            conflict_token=described.conflict_token,
            identity="tests",
            ignore_missing_task_queues=True,
        )
    )


@pytest_asyncio.fixture(loop_scope="module")
async def worker(env, settings):
    """Production's Worker, built by production's code path."""
    w = runtime.build_worker(env.client, [HelloWorkflow], [say_hello], settings)
    async with w:
        await make_current(env.client, settings.deployment_name, settings.build_id)
        yield w


@pytest.fixture
def wf_id():
    return f"test-{uuid.uuid4()}"
