"""targets.py: what `--worker X` means. No server, no cloud, no network."""

import ast
import pathlib

import pytest

import targets as T


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def root(tmp_path):
    write(
        tmp_path,
        "agentcore/terraform.tfvars",
        'temporal_namespace = "ns-a.abc12"\n'
        'temporal_address   = "ns-a.abc12.tmprl.cloud:7233"\n'
        'build_id = "1.0.3"  # comment\n'
        'retained_build_ids = ["1.0.0"]\n',
    )
    return tmp_path


def test_agentcore_reads_the_existing_tfvars(root):
    t = T.resolve("agentcore", {}, root)
    assert (t.namespace, t.address) == ("ns-a.abc12", "ns-a.abc12.tmprl.cloud:7233")
    assert t.deployment == "research-fleet-agentcore" and t.tls and t.needs_api_key


def test_fleet_toml_beats_tfvars(root):
    t = T.resolve("agentcore", {"agentcore": {"namespace": "other.xyz", "address": "other.xyz.tmprl.cloud:7233"}}, root)
    assert t.namespace == "other.xyz"


def test_agentcore_refuses_a_self_hosted_address(root):
    with pytest.raises(T.TargetError, match="tmprl.cloud"):
        T.resolve("agentcore", {"agentcore": {"address": "localhost:7233"}}, root)


def test_agentcore_without_a_namespace_says_where_to_set_it(tmp_path):
    with pytest.raises(T.TargetError, match="fleet.toml"):
        T.resolve("agentcore", {}, tmp_path)


def test_cloudrun_goes_through_the_tunnel_with_the_prefix_as_deployment(tmp_path):
    t = T.resolve("cloudrun", {"cloudrun": {"frontend_port": 7433, "prefix": "demo2"}}, tmp_path)
    assert t.address == "localhost:7433" and t.deployment == "demo2" and not t.tls


def test_precedence_flag_env_saved_config_default(tmp_path):
    cfg = {"defaults": {"worker": "cloudrun"}}
    assert T.pick_worker(None, {}, {}, tmp_path) == "local"
    assert T.pick_worker(None, {}, cfg, tmp_path) == "cloudrun"
    write(tmp_path, ".fleet/target", "agentcore\n")
    assert T.pick_worker(None, {}, cfg, tmp_path) == "agentcore"
    assert T.pick_worker(None, {"FLEET_WORKER": "local"}, cfg, tmp_path) == "local"
    assert T.pick_worker("cloudrun", {"FLEET_WORKER": "local"}, cfg, tmp_path) == "cloudrun"


def test_unknown_worker_is_refused(tmp_path):
    with pytest.raises(T.TargetError, match="unknown worker"):
        T.pick_worker("lambda", {}, {}, tmp_path)


def test_ambient_temporal_vars_never_reach_the_child(root):
    """A shell left on Temporal Cloud must not redirect a cloudrun command, and a Cloud
    API key must not be sent to the self-hosted frontend."""
    shell = {
        "TEMPORAL_ADDRESS": "ns.tmprl.cloud:7233", "TEMPORAL_NAMESPACE": "prod",
        "TEMPORAL_API_KEY": "sk-should-not-leak", "TEMPORAL_TLS": "true", "PATH": "/bin",
    }
    env = T.child_env(shell, T.resolve("cloudrun", {}, root), root)
    assert env["TEMPORAL_ADDRESS"] == "localhost:7233"
    assert env["TEMPORAL_NAMESPACE"] == "default"
    assert env["TEMPORAL_TLS"] == "false"
    assert "TEMPORAL_API_KEY" not in env and env["PATH"] == "/bin"


def test_agentcore_child_gets_the_key_and_tls(root):
    env = T.child_env({"TEMPORAL_API_KEY": "k"}, T.resolve("agentcore", {}, root), root)
    assert env["TEMPORAL_API_KEY"] == "k" and env["TEMPORAL_TLS"] == "true"
    assert env["FLEET_TARGET"] == "agentcore"


def test_agentcore_without_a_key_refuses(root):
    with pytest.raises(T.TargetError, match="TEMPORAL_API_KEY"):
        T.child_env({}, T.resolve("agentcore", {}, root), root)


def test_dotenv_supplies_keys_without_overriding_the_shell(root):
    write(root, ".env", "TEMPORAL_API_KEY=from-dotenv\nGEMINI_API_KEY='g'\nANTHROPIC_API_KEY=a\n")
    env = T.child_env({"ANTHROPIC_API_KEY": "shell"}, T.resolve("agentcore", {}, root), root)
    assert env["TEMPORAL_API_KEY"] == "from-dotenv"
    assert env["GEMINI_API_KEY"] == "g" and env["ANTHROPIC_API_KEY"] == "shell"


def test_targets_imports_no_temporalio():
    tree = ast.parse(pathlib.Path("targets.py").read_text())
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert "temporalio" not in names
