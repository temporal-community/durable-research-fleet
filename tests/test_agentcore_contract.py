"""Guards for the AgentCore deployment target, in the style of test_serverless_contract.

Each of these fails SILENTLY in production: the stack applies, sessions start, and the
demo does something subtly wrong.
"""

import pathlib

import worker_agentcore
from agentcore_runtime import AgentCoreWorker
from research_workflow import ResearchWorkflow
from workflows import HelloWorkflow


def test_every_platform_registers_the_same_things():
    """One list (apps.py), used by all three entrypoints, or a Worker can lack an Activity
    on exactly one platform.
    """
    import apps

    assert HelloWorkflow in apps.WORKFLOWS and ResearchWorkflow in apps.WORKFLOWS
    assert apps.ACTIVITIES
    assert isinstance(worker_agentcore.worker, AgentCoreWorker)
    assert worker_agentcore.worker._workflows is apps.WORKFLOWS
    assert worker_agentcore.worker._activities is apps.ACTIVITIES
    for entry in ("worker_local.py", "worker_cloudrun.py"):
        src = pathlib.Path(entry).read_text()
        assert "from apps import ACTIVITIES, WORKFLOWS" in src, entry
        assert "say_hello" not in src, f"{entry} must not carry its own registration list"


def test_apps_py_ships_in_both_artifacts():
    assert "apps.py" in pathlib.Path("Dockerfile").read_text()
    assert "apps.py" in pathlib.Path("agentcore/build.sh").read_text()


def test_the_worker_is_built_by_the_shared_runtime():
    """build_worker is the one place that sets Worker Versioning and the slot limit; a
    second Worker constructor here would be a second place to forget use_worker_versioning.
    """
    src = pathlib.Path("agentcore_runtime.py").read_text()
    assert "runtime.build_worker(" in src
    assert "Worker(" not in src.replace("build_worker(", "").replace("AgentCoreWorker(", "")


def test_web_never_imports_the_worker_side():
    """The web tier is a Temporal CLIENT; it must not pull in the worker side or the AWS SDK."""
    src = pathlib.Path("web.py").read_text()
    for forbidden in ("agentcore_runtime", "worker_agentcore", "bedrock_agentcore", "boto3"):
        assert f"import {forbidden}" not in src and f"from {forbidden}" not in src


def test_llm_still_imports_no_temporal_or_aws():
    src = pathlib.Path("llm.py").read_text()
    for forbidden in ("temporalio", "boto3", "bedrock_agentcore"):
        assert f"import {forbidden}" not in src and f"from {forbidden}" not in src


def test_the_zip_is_built_for_the_runtime_platform_not_this_machine():
    """pip on a Mac ships darwin wheels that fail at import inside the session."""
    sh = pathlib.Path("agentcore/build.sh").read_text()
    assert "aarch64-manylinux2014" in sh
    assert "--python-version 3.12" in sh
    assert "--only-binary :all:" in sh  # never compile for the wrong platform


def test_the_zip_ships_no_web_tier():
    """Join the backslash continuations: the file list spans several physical lines."""
    sh = pathlib.Path("agentcore/build.sh").read_text().replace("\\\n", " ")
    copies = [l for l in sh.splitlines() if l.lstrip().startswith("cp ")]
    assert copies, "no cp statement found; the guard would pass vacuously"
    assert not any("web" in l for l in copies)


def test_register_refuses_a_foreign_namespace():
    sh = pathlib.Path("agentcore/register.sh").read_text()
    assert "refusing: TEMPORAL_NAMESPACE" in sh


def test_the_cloud_run_image_does_not_carry_the_agentcore_sdk():
    assert "bedrock" not in pathlib.Path("requirements.txt").read_text().lower()
    assert "worker_agentcore" not in pathlib.Path("Dockerfile").read_text()


def test_apply_and_plan_run_the_unpinned_change_guard():
    """Code change + same build_id publishes a Runtime version no endpoint references."""
    mk = pathlib.Path("Makefile").read_text()
    for target in ("ac-plan", "ac-apply"):
        block = mk.split(f"{target}:")[1].split("\n\n")[0]
        assert "agentcore/guard.sh" in block, target


def test_prune_is_read_only():
    mk = pathlib.Path("Makefile").read_text()
    block = mk.split("ac-prune:")[1].split("\n\n")[0]
    assert "register.sh" not in block, "a list target must not mutate Temporal Cloud"


def test_published_zips_are_append_only():
    """Terraform replacing the S3 object would delete the zip an older, still-pinned Runtime
    version was built from. guard.sh must forget it from state instead.
    """
    sh = pathlib.Path("agentcore/guard.sh").read_text()
    assert "state rm aws_s3_object.code" in sh
    assert "stays in S3" in sh
