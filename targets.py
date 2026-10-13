"""Where the fleet CLI points: one resolved `Target` per platform.

The app does not change between platforms. What changes is WHICH Temporal the console and
starter talk to, and that used to be spread over `.env`, `agentcore/terraform.tfvars`,
`terraform output` and whichever Make target you remembered. This module is the one place
that answers "what does `--worker agentcore` mean?".

Rules, each a trap this repo has already hit:
- Ambient `TEMPORAL_*` variables are IGNORED. A shell left pointing at Temporal Cloud must
  not silently redirect a `cloudrun` command, so the child environment is built from the
  target alone (`connection_env`).
- The API key reaches only a target that needs one, and TLS follows the target, not the key.
- Imports no `temporalio`: unit-testable with no server (enforced by
  tests/test_serverless_contract.py, like llm.py).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = "fleet.toml"
STATE_FILE = Path(".fleet") / "target"

WORKERS = ("local", "cloudrun", "agentcore")
DEFAULT_WORKER = "local"
DEFAULT_QUEUE = "research-queue"


class TargetError(Exception):
    """A configuration problem the user can fix; the message says how."""


@dataclass(frozen=True)
class Target:
    name: str
    address: str
    namespace: str
    task_queue: str
    deployment: str
    tls: bool
    needs_api_key: bool
    # What a developer should know before trusting the footer counter on this platform.
    note: str = ""

    def describe(self) -> str:
        return f"{self.name} | {self.namespace} @ {self.address} | deployment {self.deployment}"


def load_config(root: Path = ROOT) -> dict:
    """`fleet.toml` if present. Holds no secrets: project ids, a Namespace, a prefix."""
    path = root / CONFIG_FILE
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise TargetError(f"{CONFIG_FILE} is not valid TOML: {exc}") from exc


def read_tfvars(path: Path) -> dict[str, str]:
    """Scalar `key = "value"` lines of a tfvars file. Enough for namespace and build id."""
    if not path.exists():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text().splitlines():
        m = re.match(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"\s*(#.*)?$', line)
        if m:
            found[m.group(1)] = m.group(2)
    return found


def saved_worker(root: Path = ROOT) -> str | None:
    path = root / STATE_FILE
    return path.read_text().strip() or None if path.exists() else None


def pick_worker(
    flag: str | None, env: dict[str, str], config: dict, root: Path = ROOT
) -> str:
    """flag > FLEET_WORKER > `fleet use` > fleet.toml [defaults] > local."""
    chosen = (
        flag
        or env.get("FLEET_WORKER")
        or saved_worker(root)
        or config.get("defaults", {}).get("worker")
        or DEFAULT_WORKER
    )
    chosen = chosen.strip().lower()
    if chosen not in WORKERS:
        raise TargetError(f"unknown worker {chosen!r}; choose one of: {', '.join(WORKERS)}")
    return chosen


def resolve(
    worker: str, config: dict | None = None, root: Path = ROOT
) -> Target:
    """Build the Target for `worker` from fleet.toml, falling back to existing files."""
    config = load_config(root) if config is None else config
    if worker == "local":
        section = config.get("local", {})
        return Target(
            name="local",
            address=section.get("address", "localhost:7233"),
            namespace=section.get("namespace", "default"),
            task_queue=section.get("task_queue", DEFAULT_QUEUE),
            deployment=section.get("deployment", "research-fleet"),
            tls=False,
            needs_api_key=False,
            note="`temporal server start-dev` plus `python worker_local.py`; slots are raised locally.",
        )

    if worker == "cloudrun":
        section = config.get("cloudrun", {})
        port = int(section.get("frontend_port", 7233))
        prefix = section.get("prefix", "research-fleet")
        return Target(
            name="cloudrun",
            # The self-hosted frontend is private; the only way in is the SSH tunnel.
            address=f"localhost:{port}",
            namespace=section.get("namespace", "default"),
            task_queue=section.get("task_queue", DEFAULT_QUEUE),
            # var.deployment_name is the prefix in the Make wiring (see Makefile TF_VARS).
            deployment=section.get("deployment", prefix),
            tls=False,
            needs_api_key=False,
            note="One slot per instance, so workers track the fan-out width.",
        )

    if worker == "agentcore":
        section = config.get("agentcore", {})
        tfvars = read_tfvars(root / "agentcore" / "terraform.tfvars")
        namespace = section.get("namespace") or tfvars.get("temporal_namespace")
        if not namespace:
            raise TargetError(
                "agentcore needs a Namespace: set [agentcore] namespace in fleet.toml "
                "or temporal_namespace in agentcore/terraform.tfvars"
            )
        address = section.get("address") or tfvars.get("temporal_address") or f"{namespace}.tmprl.cloud:7233"
        if not address.split(":")[0].endswith(".tmprl.cloud"):
            raise TargetError(
                f"agentcore runs on Temporal Cloud; {address!r} is not a *.tmprl.cloud address"
            )
        return Target(
            name="agentcore",
            address=address,
            namespace=namespace,
            task_queue=section.get("task_queue", tfvars.get("task_queue", DEFAULT_QUEUE)),
            deployment=section.get(
                "deployment", tfvars.get("deployment_name", "research-fleet-agentcore")
            ),
            tls=True,
            needs_api_key=True,
            note="Temporal decides the session count (2-4 seen for 5-6 sub-questions), "
            "so do not expect one worker per sub-question.",
        )

    raise TargetError(f"unknown worker {worker!r}; choose one of: {', '.join(WORKERS)}")


def connection_env(target: Target, api_key: str | None, provider: str | None = None) -> dict[str, str]:
    """The env for a child process (console, starter, scripts). Built from the target ONLY.

    Raises if the target needs a key and none was given. For a target that does not, no
    key is passed at all, so a Cloud key in the shell cannot leak into a self-hosted call.
    """
    env = {
        "TEMPORAL_ADDRESS": target.address,
        "TEMPORAL_NAMESPACE": target.namespace,
        "TEMPORAL_TASK_QUEUE": target.task_queue,
        "TEMPORAL_DEPLOYMENT_NAME": target.deployment,
        "TEMPORAL_TLS": "true" if target.tls else "false",
        "FLEET_TARGET": target.name,
    }
    if target.needs_api_key:
        if not api_key:
            raise TargetError(
                f"{target.name} needs TEMPORAL_API_KEY (a key scoped to ONE Namespace); "
                "put it in .env"
            )
        env["TEMPORAL_API_KEY"] = api_key
    return env


def load_dotenv(root: Path = ROOT) -> dict[str, str]:
    """Minimal `.env` reader (KEY=VALUE, `#` comments). Never exported wholesale."""
    path = root / ".env"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def child_env(base: dict[str, str], target: Target, root: Path = ROOT) -> dict[str, str]:
    """`base` (usually os.environ) with every ambient TEMPORAL_* removed and the target's added.

    Model keys from `.env` are added when not already set, so `fleet run` works from a
    fresh shell; they are not Temporal settings and are the same on every target.
    """
    dotenv = load_dotenv(root)
    env = {k: v for k, v in base.items() if not k.startswith("TEMPORAL_")}
    for key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "DEMO_PASSCODE"):
        if key not in env and dotenv.get(key):
            env[key] = dotenv[key]
    api_key = base.get("TEMPORAL_API_KEY") or dotenv.get("TEMPORAL_API_KEY")
    env.update(connection_env(target, api_key))
    return env

