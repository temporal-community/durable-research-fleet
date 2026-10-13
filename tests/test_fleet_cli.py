"""fleet.py: the exact commands and environment each subcommand produces.

A fake runner records argv/env, so nothing executes: no Make, no gcloud, no Temporal.
"""

import io

import pytest

import fleet as F

TFVARS = (
    'temporal_namespace = "ns-a.abc12"\n'
    'build_id = "1.0.3"\n'
    'retained_build_ids = ["1.0.0", "1.0.1"]\n'
)


class Recorder:
    def __init__(self, outputs=None):
        self.calls = []
        self.outputs = outputs or {}

    def __call__(self, argv, env, capture):
        self.calls.append((argv, env))
        for prefix, out in self.outputs.items():
            if " ".join(argv).startswith(prefix):
                return 0, out
        return 0, ""

    @property
    def argvs(self):
        return [" ".join(a) for a, _ in self.calls]


@pytest.fixture
def root(tmp_path):
    (tmp_path / "agentcore").mkdir()
    (tmp_path / "agentcore" / "terraform.tfvars").write_text(TFVARS)
    (tmp_path / ".env").write_text("TEMPORAL_API_KEY=k\n")
    (tmp_path / "fleet.toml").write_text(
        '[cloudrun]\nproject = "p1"\nregion = "us-central1"\nprefix = "rf"\nbuild_id = "v1"\n'
    )
    return tmp_path


def go(root, argv, rec=None, env=None):
    rec = rec or Recorder()
    out = io.StringIO()
    rc = F.main(argv, runner=rec, env=env or {}, root=root, out=out)
    return rc, rec, out.getvalue()


def test_ask_targets_the_right_namespace_and_queue(root):
    rc, rec, _ = go(root, ["ask", "why?", "-w", "agentcore", "-p", "gemini"])
    argv, env = rec.calls[0]
    assert rc == 0 and argv[:4] == ["temporal", "workflow", "start", "--type"]
    assert '"provider": "gemini"' in argv[argv.index("--input") + 1]
    assert env["TEMPORAL_NAMESPACE"] == "ns-a.abc12" and env["TEMPORAL_API_KEY"] == "k"


def test_claude_is_spelled_anthropic_in_workflow_input(root):
    _, rec, _ = go(root, ["ask", "q", "-w", "agentcore"])
    assert '"provider": "anthropic"' in rec.calls[0][0][-1]


def test_unknown_provider_fails_cleanly(root):
    rc, rec, _ = go(root, ["ask", "q", "-w", "agentcore", "-p", "gpt"])
    assert rc == 2 and not rec.calls


def test_dry_run_runs_nothing_that_mutates(root):
    rc, rec, out = go(root, ["up", "-w", "agentcore", "--dry-run"])
    assert rc == 0 and not rec.calls and "would run: make ac-apply" in out


def test_agentcore_up_sequence(root):
    _, rec, _ = go(root, ["up", "-w", "agentcore", "-y"])
    assert rec.argvs == ["make ac-apply AUTO=1", "make ac-secrets", "make ac-register"]


def test_cloudrun_up_forwards_make_vars(root):
    _, rec, _ = go(root, ["up", "-w", "cloudrun"])
    assert rec.argvs == ["make up PROJECT=p1 REGION=us-central1 PREFIX=rf BUILD_ID=v1"]


def test_down_requires_the_typed_name(root):
    rc, rec, _ = go(root, ["down", "-w", "agentcore"])
    assert rc == 2 and not rec.calls
    rc, rec, _ = go(root, ["down", "-w", "agentcore", "--confirm", "agentcore"])
    assert rec.argvs == ["make ac-down AUTO=1"]


def test_ship_agentcore_retains_the_old_build_before_applying(root):
    rec = Recorder({"temporal workflow count": "Total: 0"})
    rc, rec, _ = go(root, ["ship", "-w", "agentcore", "-y"], rec)
    text = (root / "agentcore" / "terraform.tfvars").read_text()
    assert rc == 0
    assert 'build_id = "1.0.4"' in text
    assert '"1.0.0", "1.0.1", "1.0.3"' in text
    assert rec.argvs[-2:] == ["make ac-apply AUTO=1", "make ac-register"]
    assert "SET_CURRENT" not in " ".join(rec.argvs)  # never auto-promotes


def test_ship_refuses_while_workflows_are_running(root):
    rec = Recorder({"temporal workflow count": "Total: 2"})
    rc, rec, _ = go(root, ["ship", "-w", "agentcore"], rec)
    assert rc == 2 and "make ac-apply" not in rec.argvs
    assert "1.0.3" in (root / "agentcore" / "terraform.tfvars").read_text()
    assert 'build_id = "1.0.3"' in (root / "agentcore" / "terraform.tfvars").read_text()


def test_ship_refuses_when_it_cannot_count(root):
    class Broken(Recorder):
        def __call__(self, argv, env, capture):
            return 1, ""

    rc, _, _ = go(root, ["ship", "-w", "agentcore"], Broken())
    assert rc == 2


def test_promote_sets_current_only_on_request(root):
    _, rec, _ = go(root, ["ship", "-w", "agentcore", "--promote"])
    assert rec.argvs == ["make ac-register SET_CURRENT=1"]


def test_ship_cloudrun_forces_a_pool_revision_by_digest(root):
    rec = Recorder({"temporal workflow count": "0", "gcloud artifacts": "sha256:abc\n"})
    rc, rec, _ = go(root, ["ship", "-w", "cloudrun"], rec)
    assert rc == 0
    last = rec.argvs[-1]
    assert last.startswith("gcloud run worker-pools update rf-worker-pool")
    assert "us-central1-docker.pkg.dev/p1/rf-repo/research-worker@sha256:abc" in last


def test_verify_picks_the_platform_script(root):
    assert go(root, ["verify", "-w", "agentcore"])[1].argvs == ["make ac-verify"]


def test_bump_and_tfvars_edit():
    assert F.bump("1.0.3") == "1.0.4" and F.bump("v1") == "v2"
    with pytest.raises(F.FleetError):
        F.bump("latest")
    out = F.edit_agentcore_tfvars('build_id = "2"\n', "2", "3")
    assert 'build_id = "3"' in out and 'retained_build_ids = ["2"]' in out


def test_use_saves_and_is_then_the_default(root):
    go(root, ["use", "agentcore"])
    _, rec, _ = go(root, ["verify"])
    assert rec.argvs == ["make ac-verify"]


def test_missing_agentcore_key_is_a_clear_error(root):
    (root / ".env").unlink()
    rc, rec, _ = go(root, ["ask", "q", "-w", "agentcore"])
    assert rc == 2 and not rec.calls
