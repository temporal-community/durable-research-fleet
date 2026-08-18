"""Guards on the Serverless Workers requirements from docs/KNOWLEDGE_BASE.md.

These are the rules that, when broken, fail *silently* — the stack still deploys
and then either hangs or never scales. Each test names the KB section it enforces
so the link back to the research is explicit.
"""

import inspect
import pathlib

import pytest
from temporalio import workflow as temporal_workflow
from temporalio.common import VersioningBehavior

import activities
import research_activities
import research_workflow
import runtime
import workflows

# Both apps registered on the pool. The contract applies to every one of them, so
# these are parameterised rather than written against the hello app alone.
APP_MODULES = [workflows, research_workflow]


def test_cli_build_pins_cloud_run_field_mask_fix():
    """The CLI's July scaler dependency sent a camelCase gRPC field mask.

    Cloud Run accepted UpdateWorkerPool but silently ignored the requested count,
    so the WCI believed it had scaled while the pool stayed at zero. Keep the
    upstream revision containing the snake_case mask fix pinned until CLI main
    advances past it.
    """
    makefile = pathlib.Path("Makefile").read_text()
    assert "v0.0.0-20260811170210-91f6fe1d10ab" in makefile
    assert "go get go.temporal.io/auto-scaled-workers@$(AUTO_SCALED_WORKERS_VERSION)" in makefile


# --- KB §5: Worker Versioning is mandatory ---------------------------------


@pytest.mark.parametrize("module", APP_MODULES, ids=lambda m: m.__name__)
def test_every_workflow_declares_a_versioning_behavior(module):
    """KB §5. Serverless Workers require Worker Versioning; a Workflow without a
    versioning behavior is rejected at registration time.
    """
    found = [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if getattr(obj, "__temporal_workflow_definition", None) is not None
    ]
    assert found, f"no Workflow definitions found in {module.__name__}"

    for wf in found:
        d = temporal_workflow._Definition.must_from_class(wf)
        assert d.versioning_behavior not in (
            None,
            VersioningBehavior.UNSPECIFIED,
        ), f"{wf.__name__} must declare PINNED or AUTO_UPGRADE (KB §5)"


def test_both_apps_are_registered_on_the_pool():
    """The hello app is the infrastructure smoke test and must stay registered: it
    is the only path `make verify SCALE=1` can exercise without either model key.
    """
    for path in ("worker_cloudrun.py", "worker_local.py"):
        src = open(path).read()
        assert "HelloWorkflow" in src, f"{path} dropped the smoke-test app"
        assert "ResearchWorkflow" in src, f"{path} dropped the research app"
        assert "RESEARCH_ACTIVITIES" in src


def test_worker_runs_in_versioned_mode():
    """KB §5. If the Workflow declares a versioning behavior but the Worker is
    unversioned, the server rejects every Workflow Task with "versioning behavior
    cannot be specified without deployment options being set with versioned mode"
    and the Workflow retries forever.
    """
    src = inspect.getsource(runtime.build_worker)
    assert "use_worker_versioning=True" in src
    assert "WorkerDeploymentConfig" in src


# --- KB §4 + §7.2: slot limits are what make backlog (and scaling) exist ---


def test_activity_slots_default_to_one(monkeypatch):
    """KB §4 (slot isolation) and §7.2 (scaling reacts to Task Queue backlog).

    The SDK default is 100. At that level one instance absorbs a whole burst, no
    backlog forms, and the Worker Controller has no signal to scale on — the demo
    silently does nothing.
    """
    monkeypatch.delenv("MAX_CONCURRENT_ACTIVITIES", raising=False)
    assert runtime.Settings.from_env().max_concurrent_activities == 1


def test_slot_limit_is_actually_passed_to_the_worker():
    src = inspect.getsource(runtime.build_worker)
    assert "max_concurrent_activities=settings.max_concurrent_activities" in src


# --- KB §7.3: pool-level scale-in can stop a busy instance -----------------


def test_activity_heartbeats():
    """KB §7.3. Scale-in is decided at the pool level and does not know which
    instance is mid-Activity, so Activities must heartbeat to be recoverable.
    """
    src = inspect.getsource(activities.say_hello)
    assert "activity.heartbeat" in src


def test_long_activities_heartbeat_on_a_timer_not_only_between_rounds():
    """KB §7.3, sharpened for LLM Activities.

    The hello Activity can heartbeat inside its own sleep loop. A research Activity
    awaits a single API call that may run for a minute or more, so it must heartbeat
    while that call is in flight or a healthy Activity can exceed heartbeat_timeout
    and be killed.
    """
    assert "_heartbeating" in inspect.getsource(research_activities.research_subquestion)
    src = inspect.getsource(research_activities._heartbeating)
    assert "asyncio.sleep(interval)" in src and "activity.heartbeat" in src


def test_long_activities_record_cancellation_before_retry():
    """KB §7.3. Cancellation must heartbeat so Temporal can retry promptly and,
    for Claude, retain the latest completed pause_turn checkpoint.
    """
    src = inspect.getsource(research_activities.research_subquestion)
    assert "CancelledError" in src and "activity.heartbeat(state)" in src


@pytest.mark.parametrize(
    "interval,timeout,start_to_close",
    [
        (
            activities.HEARTBEAT_INTERVAL_SECONDS,
            workflows.ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS,
            workflows.ACTIVITY_START_TO_CLOSE_SECONDS,
        ),
        (
            research_activities.HEARTBEAT_INTERVAL_SECONDS,
            research_workflow.RESEARCH_HEARTBEAT_TIMEOUT_SECONDS,
            research_workflow.RESEARCH_START_TO_CLOSE_SECONDS,
        ),
    ],
    ids=["hello", "research"],
)
def test_heartbeat_invariant_holds_for_every_app(interval, timeout, start_to_close):
    """KB §7.3. Heartbeats only help if the Workflow sets heartbeat_timeout, and the
    ordering must hold for EVERY app on the pool — the research Activity's timeouts
    are 20x the hello app's, so a single hardcoded assertion would not have caught a
    mistake in either one.
    """
    assert interval < timeout < start_to_close


def test_workflows_set_a_heartbeat_timeout():
    for wf in (workflows.HelloWorkflow, research_workflow.ResearchWorkflow):
        assert "heartbeat_timeout" in inspect.getsource(wf), wf.__name__


def test_worker_shuts_down_gracefully():
    """KB §7.3. Cloud Run sends SIGTERM on scale-in. temporalio installs no signal
    handler, so without this the process dies instantly and abandons the Activity.
    """
    assert "graceful_shutdown_timeout" in inspect.getsource(runtime.build_worker)
    assert "SIGTERM" in inspect.getsource(runtime.run_worker)


# --- KB §7.1: ordinary long-polling Worker, not a Lambda-style adapter -----


def test_worker_is_long_polling_not_per_invocation():
    """KB §7.1. On Cloud Run the WCI scales the *number* of long-lived instances;
    it does not invoke the process per task. A Lambda-style exit-after-batch loop
    would be wrong here.
    """
    src = inspect.getsource(runtime.run_worker)
    assert "async with worker" in src
    assert "await stop.wait()" in src


# --- Configuration contract that Terraform depends on ---------------------


@pytest.mark.parametrize(
    "env_value,expect_tls",
    [(None, False), ("true", True), ("false", False), ("TRUE", True)],
)
def test_tls_resolution(monkeypatch, env_value, expect_tls):
    """A self-hosted frontend is plaintext gRPC; Temporal Cloud needs TLS. Getting
    this wrong fails the handshake with a confusing transport error.
    """
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    monkeypatch.delenv("TEMPORAL_TLS", raising=False)
    if env_value is not None:
        monkeypatch.setenv("TEMPORAL_TLS", env_value)
    assert runtime.Settings.from_env().tls is expect_tls


def test_tls_defaults_on_when_an_api_key_is_present(monkeypatch):
    monkeypatch.delenv("TEMPORAL_TLS", raising=False)
    monkeypatch.setenv("TEMPORAL_API_KEY", "fake-key")
    s = runtime.Settings.from_env()
    assert s.tls is True and s.api_key == "fake-key"


def test_deployment_name_comes_from_the_environment(monkeypatch):
    """Terraform owns this value. If the worker hardcodes it, Terraform can attach
    the compute config to one deployment while the pool registers under another,
    and Workflows hang with no error at all.
    """
    monkeypatch.setenv("TEMPORAL_DEPLOYMENT_NAME", "research-agent")
    assert runtime.Settings.from_env().deployment_name == "research-agent"


def test_deployment_name_is_not_hardcoded_in_the_workers():
    for mod_src in ("worker_cloudrun.py", "worker_local.py"):
        src = open(mod_src).read()
        assert "deployment_name=" not in src, f"{mod_src} must not hardcode deployment_name"


def test_bad_integer_env_falls_back_instead_of_crashing(monkeypatch):
    """A typo'd env var must not crash-loop every instance in the pool."""
    monkeypatch.setenv("MAX_CONCURRENT_ACTIVITIES", "not-a-number")
    assert runtime.Settings.from_env().max_concurrent_activities == 1


def test_runtime_is_app_agnostic():
    """The seam the Research agent app plugs into: runtime.py must not IMPORT the
    app modules, or swapping the app means editing the infra.

    Checked via the import graph, not raw text — `workflows` and `activities` are
    legitimate *parameter* names in run_worker(), which is the whole point.
    """
    import ast

    tree = ast.parse(inspect.getsource(runtime))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    app_modules = {
        "workflows",
        "activities",
        "research_workflow",
        "research_activities",
        "research_types",
        "llm",
    }
    leaked = imported & app_modules
    assert not leaked, f"runtime.py must not import app modules, found: {leaked}"


def test_the_web_tier_does_not_import_the_research_app():
    """`web.py` runs in the same image but must not pull in either model SDK.

    It starts, queries and signals Workflows by NAME, so the request-serving process
    needs no GEMINI_API_KEY and no shared types to keep in sync. An import here
    would couple the two tiers for no benefit.
    """
    import ast

    tree = ast.parse(open("web.py").read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    leaked = imported & {
        "google",
        "anthropic",
        "llm",
        "research_workflow",
        "research_activities",
        "research_types",
    }
    assert not leaked, f"web.py must stay decoupled from the research app: {leaked}"


def test_the_llm_seam_does_not_import_temporal():
    """`llm.py` must stay Temporal-agnostic so it unit-tests without a server, and
    so the retry/durability story lives in exactly one place (the Workflow).
    """
    import ast

    import llm

    tree = ast.parse(inspect.getsource(llm))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        assert not any(
            n.split(".")[0] == "temporalio" for n in names
        ), "llm.py must not import temporalio"


def test_no_ambiguous_model_alias():
    """Model constants must name their provider.

    Before the dual-provider port `llm.py` exported `MODEL = "claude-opus-5"`. The port
    kept the name alive as `MODEL = GEMINI_MODEL` and labelled it backwards-compatible,
    which is the opposite of what it was: the same public symbol silently changed from a
    Claude id to a Gemini id, so any caller reaching for it kept working and started
    talking to the other provider. An ImportError is the better failure — it names the
    line to fix instead of returning a plausible wrong model.

    Both request builders must read the explicit names, so nothing needs the alias.
    """
    import llm

    assert not hasattr(llm, "MODEL"), (
        "llm.MODEL is provider-ambiguous — it meant Claude before the dual-provider "
        "port and would mean Gemini now. Use GEMINI_MODEL or ANTHROPIC_MODEL."
    )
    assert llm.GEMINI_MODEL and llm.ANTHROPIC_MODEL
    assert llm.GEMINI_MODEL != llm.ANTHROPIC_MODEL

    src = inspect.getsource(llm)
    assert "model=GEMINI_MODEL" in src, "the Gemini call must name the Gemini model"
    assert '"model": ANTHROPIC_MODEL' in src, "the Claude call must name the Claude model"


def test_only_temporal_retries():
    """Two retry layers multiply into latency nobody can reason about, and a
    hand-rolled one is the thing this demo argues against. Both clients disable SDK
    retries and the Activity's RetryPolicy owns recovery, including 429s.
    """
    import llm

    assert "HttpRetryOptions(attempts=1)" in inspect.getsource(llm.client)
    assert "max_retries=0" in inspect.getsource(llm.anthropic_client)
    policy = research_workflow.RESEARCH_RETRY
    assert policy.maximum_interval.total_seconds() >= 60
    assert policy.maximum_attempts >= 5
    # An identical refused request gets declined again; retrying only burns tokens.
    assert "RefusalError" in (policy.non_retryable_error_types or [])


def test_the_page_never_uses_innerhtml():
    """The report is MODEL OUTPUT rendered as markdown, so this is the XSS boundary.

    `web/index.html` builds every node with createElement and sets every text run
    with textContent; clearing uses replaceChildren(). One `innerHTML =` on a path
    that touches a question, a sub-question, a source title or the report body turns
    an audience-supplied string into markup on a public page.

    Grep rather than a parser because the rule is absolute: there is no acceptable
    use of innerHTML in this file, including `innerHTML = ''`, so there is nothing to
    whitelist. A previous version used it to clear lists — hence replaceChildren.
    """
    page = (pathlib.Path(__file__).parent.parent / "web" / "index.html").read_text()
    offenders = [
        line.strip()
        for line in page.splitlines()
        if "innerHTML" in line and not line.strip().startswith(("//", "*", "<!--"))
    ]
    assert not offenders, f"innerHTML in web/index.html: {offenders}"


def test_the_page_validates_citation_numbers():
    """A model that miscounts must not produce a link to a source that isn't there.

    The renderer only turns `[n]` into a citation when 1 <= n <= len(sources); out of
    range stays plain text. Without the bound the page would mint dead anchors and
    the demo would show a footnote pointing at nothing.
    """
    page = (pathlib.Path(__file__).parent.parent / "web" / "index.html").read_text()
    assert "n <= nSources" in page, "citation numbers must be bounded by the source count"
    assert "safeUrl" in page, "model-supplied URLs must be scheme-checked"


def test_worker_identity_is_unique_per_process():
    """REGRESSION — the demo's headline number depended on this and was wrong on stage.

    Temporal's default identity is `{pid}@{hostname}`. In a Cloud Run container the
    worker is PID 1 and the hostname is `localhost`, so every instance reported
    `1@localhost`. web.py counts DISTINCT POLLER IDENTITIES, so the Serverless Workers
    tile read 1 while six instances were genuinely researching in parallel — verified
    from a live run's history, where six Activities started in the same second and
    overlapped yet all carried `1@localhost`.

    It worked locally (real pids, real hostnames), which is why no local test caught
    it. So this asserts uniqueness rather than format.
    """
    ids = {runtime.worker_identity() for _ in range(200)}
    assert len(ids) == 200, "identity must be unique per call, not per pid+hostname"
    # And it must actually be wired into the client, not just available.
    assert "identity=identity" in inspect.getsource(runtime.connect)
