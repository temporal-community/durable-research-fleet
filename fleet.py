"""fleet: one command surface for the research fleet, whichever platform serves it.

    ./fleet run --worker agentcore      the console, pointed at Temporal Cloud
    ./fleet run --worker cloudrun       the console, pointed at the self-hosted VM (tunnel)
    ./fleet ask "question" -w agentcore start a research Workflow from the terminal
    ./fleet status | verify | doctor | up | ship | down

The app is identical on every platform; `--worker` only selects WHERE it talks (see
targets.py). This file is a thin dispatcher: Make, the agentcore/*.sh scripts and the
`temporal` CLI stay the implementation, so nothing proven live is rewritten. Stdlib only.

Every command accepts --dry-run, which prints the exact commands instead of running them.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TextIO

import targets as T

# (argv, env, capture) -> (returncode, captured stdout)
Runner = Callable[[list[str], dict[str, str], bool], tuple[int, str]]

PROVIDERS = {"claude": "anthropic", "anthropic": "anthropic", "gemini": "gemini"}
RUNNING_RESEARCH = "WorkflowType = 'ResearchWorkflow' AND ExecutionStatus = 'Running'"


def subprocess_runner(argv: list[str], env: dict[str, str], capture: bool) -> tuple[int, str]:
    proc = subprocess.run(argv, env=env, cwd=T.ROOT, text=True, capture_output=capture)
    return proc.returncode, proc.stdout if capture else ""


@dataclass
class Ctx:
    target: T.Target
    config: dict
    env: dict[str, str]  # the caller's environment (os.environ)
    runner: Runner = subprocess_runner
    dry_run: bool = False
    root: Path = T.ROOT
    out: TextIO = field(default_factory=lambda: sys.stdout)

    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def run(self, argv: list[str], *, capture: bool = False, mutates: bool = True,
            extra_env: dict[str, str] | None = None) -> tuple[int, str]:
        """Run with the TARGET's env. A dry run prints mutating commands and skips them;
        read-only ones (`mutates=False`) still run so a dry run can show real state."""
        env = T.child_env(self.env, self.target, self.root)
        env.update(extra_env or {})
        if self.dry_run and mutates:
            self.say("  would run: " + " ".join(argv))
            return 0, ""
        return self.runner(argv, env, capture)

    # ---- Make variables for the Cloud Run stack, from fleet.toml ------------------------
    def make_vars(self) -> list[str]:
        section = self.config.get("cloudrun", {})
        pairs = {
            "PROJECT": "project", "REGION": "region", "ZONE": "zone",
            "PREFIX": "prefix", "BUILD_ID": "build_id",
        }
        return [f"{k}={section[v]}" for k, v in pairs.items() if section.get(v)]


class FleetError(Exception):
    pass


def banner(ctx: Ctx) -> None:
    ctx.say(f"fleet -> {ctx.target.describe()}")


# --------------------------------------------------------------------------- commands


def cmd_targets(ctx: Ctx, args) -> int:
    active = ctx.target.name
    for name in T.WORKERS:
        try:
            t = T.resolve(name, ctx.config, ctx.root)
            detail = f"{t.namespace} @ {t.address}  deployment {t.deployment}"
            if t.note:
                detail += f"\n{'':14}{t.note}"
        except T.TargetError as exc:
            detail = f"not configured: {exc}"
        ctx.say(f"{'*' if name == active else ' '} {name:<10} {detail}")
    return 0


def cmd_use(ctx: Ctx, args) -> int:
    T.resolve(args.worker_name, ctx.config, ctx.root)  # refuse a target that cannot resolve
    if ctx.dry_run:
        ctx.say(f"  would save default worker: {args.worker_name}")
        return 0
    path = ctx.root / T.STATE_FILE
    path.parent.mkdir(exist_ok=True)
    path.write_text(args.worker_name + "\n")
    ctx.say(f"default worker is now {args.worker_name}")
    return 0


def tunnel_up(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def require_tunnel(ctx: Ctx) -> None:
    if ctx.target.name != "cloudrun" or ctx.dry_run:
        return
    port = int(ctx.target.address.rsplit(":", 1)[1])
    if not tunnel_up(port):
        raise FleetError(
            f"nothing is listening on localhost:{port}. Start the tunnel in another "
            f"terminal:  ./fleet tunnel"
        )


def cmd_tunnel(ctx: Ctx, args) -> int:
    if ctx.target.name != "cloudrun":
        raise FleetError("the tunnel is only for --worker cloudrun")
    port = ctx.target.address.rsplit(":", 1)[1]
    return ctx.run(["make", "tunnel", f"LOCAL_FRONTEND_PORT={port}", *ctx.make_vars()])[0]


def cmd_run(ctx: Ctx, args) -> int:
    require_tunnel(ctx)
    banner(ctx)
    ctx.say(f"console: http://localhost:{args.port}")
    argv = [sys.executable, "-m", "uvicorn", "web:app", "--port", str(args.port), "--no-proxy-headers"]
    if args.reload:
        argv.append("--reload")
    if args.chaos:
        ctx.say("chaos panel ON: the console can cut the model-API route for a run (this process only)")
    return ctx.run(argv, extra_env={"FLEET_CHAOS": "1"} if args.chaos else None)[0]


def provider_name(raw: str) -> str:
    try:
        return PROVIDERS[raw.lower()]
    except KeyError:
        raise FleetError(f"unknown provider {raw!r}; choose claude or gemini") from None


def cmd_ask(ctx: Ctx, args) -> int:
    require_tunnel(ctx)
    banner(ctx)
    provider = provider_name(args.provider)
    workflow_id = f"research-{uuid.uuid4().hex[:10]}"
    payload = '{"question": %s, "provider": "%s"}' % (_json_str(args.question), provider)
    rc, _ = ctx.run([
        "temporal", "workflow", "start",
        "--type", "ResearchWorkflow",
        "--task-queue", ctx.target.task_queue,
        "--workflow-id", workflow_id,
        "--input", payload,
    ])
    if rc == 0 and not ctx.dry_run:
        ctx.say(f"started {workflow_id} with {provider}. Follow it:  ./fleet run -w {ctx.target.name}")
    return rc


def _json_str(value: str) -> str:
    import json

    return json.dumps(value)


def cmd_status(ctx: Ctx, args) -> int:
    require_tunnel(ctx)
    banner(ctx)
    rc, _ = ctx.run(["temporal", "worker", "deployment", "describe", "--name", ctx.target.deployment],
                    mutates=False)
    ctx.run(["temporal", "workflow", "count", "--query", RUNNING_RESEARCH], mutates=False)
    if ctx.target.note:
        ctx.say(f"note: {ctx.target.note}")
    return rc


def cmd_verify(ctx: Ctx, args) -> int:
    banner(ctx)
    name = ctx.target.name
    if name == "agentcore":
        return ctx.run(["make", "ac-verify"])[0]
    if name == "cloudrun":
        require_tunnel(ctx)
        return ctx.run(["make", "verify", f"SCALE={1 if args.scale else 0}", *ctx.make_vars()])[0]
    require_tunnel(ctx)
    return ctx.run([sys.executable, "starter.py", "--watch"])[0]


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def cmd_doctor(ctx: Ctx, args) -> int:
    banner(ctx)
    failures = 0

    def check(ok: bool, label: str, fix: str = "") -> None:
        nonlocal failures
        ctx.say(f"  {'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"   -> {fix}"))
        failures += 0 if ok else 1

    name = ctx.target.name
    tools = {"local": ["temporal"], "cloudrun": ["temporal", "terraform", "gcloud", "docker"],
             "agentcore": ["temporal", "terraform", "aws", "bash"]}[name]
    for tool in tools:
        check(_have(tool), f"{tool} on PATH", f"install {tool}")

    dotenv = T.load_dotenv(ctx.root)
    if ctx.target.needs_api_key:
        check(bool(dotenv.get("TEMPORAL_API_KEY") or ctx.env.get("TEMPORAL_API_KEY")),
              "TEMPORAL_API_KEY set (value never printed)", "add it to .env")
    for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        have = bool(dotenv.get(key) or ctx.env.get(key))
        ctx.say(f"  {'ok  ' if have else 'skip'}  {key} {'set' if have else 'unset: that provider will fail'}")

    if name == "cloudrun":
        port = int(ctx.target.address.rsplit(":", 1)[1])
        check(tunnel_up(port), f"tunnel on localhost:{port}", "./fleet tunnel")
    if not ctx.dry_run and _have("temporal"):
        rc, _ = ctx.run(["temporal", "worker", "deployment", "describe", "--name", ctx.target.deployment],
                        capture=True, mutates=False)
        check(rc == 0, f"Temporal reachable and deployment {ctx.target.deployment!r} exists",
              "check the Namespace, API key, tunnel, and that the Version is registered")
    ctx.say("all good" if not failures else f"{failures} problem(s)")
    return 1 if failures else 0


def running_research(ctx: Ctx) -> int | None:
    """Count of running ResearchWorkflows, or None if it could not be read."""
    rc, out = ctx.run(["temporal", "workflow", "count", "--query", RUNNING_RESEARCH],
                      capture=True, mutates=False)
    m = re.search(r"(\d+)", out or "")
    return int(m.group(1)) if rc == 0 and m else None


def refuse_if_parked(ctx: Ctx, force: bool) -> None:
    """AGENTS.md: don't deploy a new BUILD_ID while a Workflow is awaiting review (PINNED)."""
    if force or ctx.dry_run:
        return
    n = running_research(ctx)
    if n is None:
        raise FleetError("could not count running Workflows; fix connectivity or pass --force")
    if n:
        raise FleetError(
            f"{n} ResearchWorkflow(s) still running on {ctx.target.name}. They are PINNED to the "
            "current Build; let them finish, or pass --force if they are disposable."
        )


def cmd_warm(ctx: Ctx, args) -> int:
    """Have workers up BEFORE the first question. The cold start is platform provisioning.

    Measured 2026-10-13 on Cloud Run: ~125s from the controller's scale request to the first
    Worker polling, on a warm AND a brand-new revision alike, of which the container itself
    is ~6s. Nothing in this repo shortens that, but the controller scales in only ~1 instance
    a minute, so instances raised now are still there when the room starts asking.
    """
    banner(ctx)
    name = ctx.target.name
    if name == "local":
        ctx.say("local has no cold start.")
        return 0
    if name == "agentcore":
        # Any Workflow makes Temporal Cloud invoke a session; the hello app needs no model key.
        rc, _ = ctx.run(["temporal", "workflow", "start", "--type", "HelloWorkflow",
                         "--task-queue", ctx.target.task_queue,
                         "--workflow-id", f"warm-{uuid.uuid4().hex[:8]}", "--input", '"warm"'])
        if rc == 0 and not ctx.dry_run:
            ctx.say("a session starts in ~20s and stays up for the debounce window.")
        return rc
    sec = ctx.config.get("cloudrun", {})
    project, region = sec.get("project"), sec.get("region", "us-east4")
    if not project:
        raise FleetError("set [cloudrun] project in fleet.toml")
    pool = f"{sec.get('prefix', 'research-fleet')}-worker-pool"
    rc, _ = ctx.run(["gcloud", "run", "worker-pools", "update", pool, "--instances", str(args.count),
                     "--region", region, "--project", project])
    if rc == 0 and not ctx.dry_run:
        ctx.say(f"asked for {args.count} instances. Allow ~2-3 minutes before the first question; "
                "the controller then scales in about one instance a minute.")
    return rc


def cmd_up(ctx: Ctx, args) -> int:
    banner(ctx)
    auto = ["AUTO=1"] if args.yes else []
    name = ctx.target.name
    if name == "local":
        ctx.say("local needs no stack: temporal server start-dev, then python worker_local.py (see README).")
        return 0
    if name == "cloudrun":
        return ctx.run(["make", "up", *auto, *ctx.make_vars()])[0]
    for step in (["make", "ac-apply", *auto], ["make", "ac-secrets"], ["make", "ac-register"]):
        rc, _ = ctx.run(step)
        if rc:
            return rc
    if ctx.dry_run:
        return 0
    ctx.say("registered. Verify, then route traffic:  ./fleet verify -w agentcore  &&  ./fleet ship -w agentcore --promote")
    return 0


def cmd_down(ctx: Ctx, args) -> int:
    banner(ctx)
    name = ctx.target.name
    if name == "local":
        raise FleetError("nothing to destroy for local")
    if not ctx.dry_run and args.confirm != name:
        raise FleetError(f"this destroys the {name} stack. Re-run with --confirm {name}")
    auto = ["AUTO=1"] if args.confirm == name else []
    if name == "cloudrun":
        return ctx.run(["make", "down", *auto, *ctx.make_vars()])[0]
    return ctx.run(["make", "ac-down", *auto])[0]


def bump(version: str) -> str:
    """1.0.3 -> 1.0.4; v1 -> v2. Anything else needs --build-id."""
    m = re.fullmatch(r"(v?)(\d+(?:\.\d+)*)", version)
    if not m:
        raise FleetError(f"cannot bump {version!r}; pass --build-id")
    parts = m.group(2).split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return m.group(1) + ".".join(parts)


def edit_agentcore_tfvars(text: str, old: str, new: str) -> str:
    """Set build_id=new and append the OLD id to retained_build_ids (so its endpoint survives)."""
    text, n = re.subn(r'^(\s*build_id\s*=\s*)"[^"]*"', rf'\1"{new}"', text, flags=re.M)
    if n != 1:
        raise FleetError("agentcore/terraform.tfvars has no single build_id line")
    m = re.search(r"^(\s*retained_build_ids\s*=\s*)\[([^\]]*)\]", text, flags=re.M)
    if m:
        ids = re.findall(r'"([^"]+)"', m.group(2))
        if old not in ids:
            ids.append(old)
        text = text[: m.start()] + m.group(1) + "[" + ", ".join(f'"{i}"' for i in ids) + "]" + text[m.end():]
    else:
        text = text.rstrip("\n") + f'\nretained_build_ids = ["{old}"]\n'
    return text


def cmd_ship(ctx: Ctx, args) -> int:
    banner(ctx)
    name = ctx.target.name
    if name == "local":
        raise FleetError("ship is for deployed targets")
    if name == "agentcore" and args.promote:
        return _promote(ctx)
    refuse_if_parked(ctx, args.force)
    if name == "agentcore":
        return _ship_agentcore(ctx, args)
    return _ship_cloudrun(ctx, args)


def _promote(ctx: Ctx) -> int:
    ctx.say("routing new Workflows to the registered Version (existing ones stay pinned)")
    return ctx.run(["make", "ac-register", "SET_CURRENT=1"])[0]


def _ship_agentcore(ctx: Ctx, args) -> int:
    path = ctx.root / "agentcore" / "terraform.tfvars"
    old = T.read_tfvars(path).get("build_id")
    if not old:
        raise FleetError("agentcore/terraform.tfvars has no build_id")
    new = args.build_id or bump(old)
    if new == old:
        raise FleetError("the new build id equals the current one; AgentCore would publish an unreferenced version")
    ctx.say(f"build_id {old} -> {new}; {old} joins retained_build_ids (its endpoint must survive)")
    if not ctx.dry_run:
        path.write_text(edit_agentcore_tfvars(path.read_text(), old, new))
    for step in (["make", "ac-apply", *(["AUTO=1"] if args.yes else [])], ["make", "ac-register"]):
        rc, _ = ctx.run(step)
        if rc:
            return rc
    if ctx.dry_run:
        return 0
    ctx.say("registered but NOT current. Verify, then:  ./fleet ship -w agentcore --promote")
    ctx.say("Later, once `make ac-prune` shows the old Version drained, remove it from retained_build_ids.")
    return 0


def _ship_cloudrun(ctx: Ctx, args) -> int:
    sec = ctx.config.get("cloudrun", {})
    project, region = sec.get("project"), sec.get("region", "us-east4")
    prefix, build = sec.get("prefix", "research-fleet"), args.build_id or sec.get("build_id", "v1")
    if not project:
        raise FleetError("set [cloudrun] project in fleet.toml")
    mv = [v for v in ctx.make_vars() if not v.startswith("BUILD_ID=")] + [f"BUILD_ID={build}"]
    rc, _ = ctx.run(["make", "image", *mv])
    if rc:
        return rc
    image = f"{region}-docker.pkg.dev/{project}/{prefix}-repo/research-worker"
    rc, digest = ctx.run(["gcloud", "artifacts", "docker", "images", "describe", f"{image}:{build}",
                          "--format=value(image_summary.digest)", "--project", project],
                         capture=True, mutates=False)
    digest = (digest or "").strip() or ("<digest>" if ctx.dry_run else "")
    if rc or not digest:
        raise FleetError("could not read the pushed image digest")
    # Gate #14: pushing the same tag does not redeploy the pool; pin the digest to force a revision.
    return ctx.run(["gcloud", "run", "worker-pools", "update", f"{prefix}-worker-pool",
                    "--image", f"{image}@{digest}", "--region", region, "--project", project])[0]


# --------------------------------------------------------------------------- wiring


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet", description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-w", "--worker", choices=T.WORKERS, help="platform (default: saved, else local)")
    common.add_argument("--dry-run", action="store_true", help="print commands instead of running them")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, parents=[common], help=help_, **kw)
        sp.set_defaults(fn=fn)
        return sp

    add("targets", cmd_targets, "list platforms and what each points at")
    add("use", cmd_use, "remember a default platform").add_argument("worker_name", choices=T.WORKERS)
    add("doctor", cmd_doctor, "preflight: tools, keys, tunnel, Temporal reachable")
    add("tunnel", cmd_tunnel, "cloudrun: SSH tunnel to the self-hosted Temporal frontend")
    sp = add("run", cmd_run, "serve the console against a platform")
    sp.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "8000")))
    sp.add_argument("--reload", action="store_true")
    sp.add_argument("--chaos", action="store_true",
                    help="show the chaos panel (cut the network mid-run). Local console only")
    sp = add("ask", cmd_ask, "start a ResearchWorkflow from the terminal")
    sp.add_argument("question")
    sp.add_argument("-p", "--provider", default="claude", help="claude or gemini")
    add("status", cmd_status, "deployment, running Workflows, platform notes")
    sp = add("verify", cmd_verify, "smoke test (no model tokens)")
    sp.add_argument("--scale", action="store_true", help="cloudrun: full scale-from-zero cycle")
    sp = add("warm", cmd_warm, "have workers up before the first question (cold start is ~2 min)")
    sp.add_argument("--count", type=int, default=6, help="cloudrun: instances to raise (default: the fan-out width)")
    sp = add("up", cmd_up, "deploy the stack")
    sp.add_argument("-y", "--yes", action="store_true", help="skip Terraform's approval prompt")
    sp = add("ship", cmd_ship, "deploy a new Build ID the careful way")
    sp.add_argument("--build-id")
    sp.add_argument("--promote", action="store_true", help="agentcore: route new Workflows to the registered Version")
    sp.add_argument("--force", action="store_true", help="ship even with Workflows running")
    sp.add_argument("-y", "--yes", action="store_true")
    sp = add("down", cmd_down, "destroy the stack")
    sp.add_argument("--confirm", metavar="WORKER", help="type the platform name to confirm")
    return p


def main(argv: list[str] | None = None, *, runner: Runner = subprocess_runner,
         env: dict[str, str] | None = None, root: Path = T.ROOT, out: TextIO | None = None) -> int:
    env = dict(os.environ) if env is None else env
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    try:
        config = T.load_config(root)
        worker = T.pick_worker(args.worker, env, config, root)
        target = T.resolve(worker, config, root)
        ctx = Ctx(target=target, config=config, env=env, runner=runner,
                  dry_run=args.dry_run, root=root, out=out)
        return args.fn(ctx, args)
    except (T.TargetError, FleetError) as exc:
        print(f"fleet: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
