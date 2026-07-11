"""Interactive driver for the gdb-MI debug tier against a live local-libvirt stack.

Collapses the build -> boot -> attach -> stopped loop you re-walk when developing or
testing new ``debug.*`` tools into single commands. A run-local dev tool (mirrors
``scripts/coverage_campaign/drive.py``); not wired into CI.

The stack must already be up (``scripts/live-stack/up.sh``). Examples::

    # one command to a stopped gdbstub session (reuses a booted Run if one exists):
    uv run python scripts/live-debug.py stopped --reuse
    # call any tool (auto-wraps the `request` arg per the tool's own schema):
    uv run python scripts/live-debug.py call debug.backtrace '{"session_id": "..."}'
    # raw gdb/MI transcript -- ground truth when a parser disagrees with gdb:
    uv run python scripts/live-debug.py transcript <session_id>
    # restart ONLY the server process to load a code change (keeps the booted VM):
    uv run python scripts/live-debug.py reload
    # release the System + its allocation when done:
    uv run python scripts/live-debug.py teardown <system_id>
    uv run python scripts/live-debug.py tools [substr]
    uv run python scripts/live-debug.py schema <tool> [...]

Auth: ``KDIVE_TOKEN`` if set, else an admin token is minted for ``--project`` (default
``demo``) via the bundled mock OIDC issuer. Base URL: ``KDIVE_STACK_BASE_URL`` or
``http://127.0.0.1:8000/mcp``.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import shutil
import subprocess  # noqa: S404 - fixed dev-tool argv, no shell except reload  # nosec B404
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from kdive.mcp.dev_harness import (
    LiveStackClient,
    mint_token,
    oidc_issuer_from_env,
)
from kdive.mcp.responses import ToolResponse

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("KDIVE_STACK_BASE_URL", "http://127.0.0.1:8000/mcp")
DEBUG_DIR = Path(os.environ.get("KDIVE_DEBUG_DIR", "/var/lib/kdive/debug"))
DEFAULT_BREAK_SYMBOL = "schedule"  # hot enough that a single -exec-continue stops at once
_POLL_INTERVAL_SEC = 5.0


@functools.cache
def _required_executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} executable is required on PATH")
    return path


def _token(project: str) -> str:
    """The caller's ``KDIVE_TOKEN``, or a freshly minted admin token for ``project``."""
    existing = os.environ.get("KDIVE_TOKEN")
    if existing:
        return existing
    return mint_token(
        oidc_issuer_from_env(),
        subject="live-debug",
        projects=[project],
        roles={project: "admin"},
        platform_roles=["platform_admin", "platform_operator"],
    )


def _as_dict(response: ToolResponse | list[ToolResponse]) -> dict[str, Any]:
    """A single ToolResponse as a plain dict (a list tool's first row, else the response)."""
    one = response[0] if isinstance(response, list) else response
    return one.model_dump(mode="json")


# --- request-wrapper resolution ------------------------------------------------------------


async def _input_schemas(client: LiveStackClient) -> dict[str, dict[str, Any]]:
    """Map every tool name to its JSON input schema (for the request-wrap decision)."""
    tools = await client._client.list_tools()  # noqa: SLF001 - dev tool reuses the harness client
    return {tool.name: dict(tool.inputSchema) for tool in tools}


def _wrap_request(schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Wrap ``args`` under ``request`` when the tool takes exactly that single object arg.

    Several read/list tools (``runs.list``, ``allocations.list``, ...) take one ``request``
    object while others take flat kwargs; this lets a caller pass the inner fields either way.
    """
    props = schema.get("properties", {})
    if set(props) == {"request"} and "request" not in args:
        return {"request": args}
    return args


async def _call(
    client: LiveStackClient, tool: str, args: dict[str, Any], schemas: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    resolved = _wrap_request(schemas.get(tool, {}), args)
    return _as_dict(await client.call_tool(tool, **resolved))


# --- polling -------------------------------------------------------------------------------


async def _poll(
    client: LiveStackClient,
    tool: str,
    args: dict[str, Any],
    schemas: dict[str, dict[str, Any]],
    *,
    done: set[str],
    timeout_sec: float,
    label: str,
) -> dict[str, Any]:
    """Poll ``tool`` until its status/state lands in ``done`` (or time out)."""
    deadline = time.monotonic() + timeout_sec
    last = None
    while True:
        envelope = await _call(client, tool, args, schemas)
        data = envelope.get("data") or {}
        state = data.get("state") or data.get("status") or envelope.get("status")
        if state != last:
            print(f"  [{label}] {state}", file=sys.stderr, flush=True)
            last = state
        if state in done:
            return envelope
        if time.monotonic() > deadline:
            raise TimeoutError(f"{label}: stuck at {state!r} after {timeout_sec:.0f}s")
        await asyncio.sleep(_POLL_INTERVAL_SEC)


_JOB_DONE = {"succeeded", "completed", "failed", "error", "cancelled"}


async def _wait_job(
    client: LiveStackClient,
    schemas: dict[str, dict[str, Any]],
    *,
    kind: str,
    timeout_sec: float,
) -> None:
    """Wait for the newest job of ``kind`` to finish.

    ``runs.get`` reports ``succeeded`` after every step, so it cannot tell build/install/boot
    apart; the per-step job status can. The step just triggered is the newest job of its kind.
    """
    jobs = await _call(client, "jobs.list", {"limit": 20}, schemas)
    job_id = next(
        (
            it["object_id"]
            for it in jobs.get("items", [])
            if (it.get("data") or {}).get("kind") == kind
        ),
        None,
    )
    if job_id is None:
        raise RuntimeError(f"no {kind} job found after triggering it")
    final = await _poll(
        client,
        "jobs.get",
        {"job_id": job_id},
        schemas,
        done=_JOB_DONE,
        timeout_sec=timeout_sec,
        label=kind,
    )
    data = final.get("data") or {}
    status = data.get("status") or data.get("state") or final.get("status")
    if status not in {"succeeded", "completed"}:
        raise RuntimeError(f"{kind} job {job_id} ended {status!r}: {final.get('detail')}")


# --- lifecycle to a stopped session --------------------------------------------------------


async def _find_booted_run(
    client: LiveStackClient, schemas: dict[str, dict[str, Any]]
) -> str | None:
    """A Run already booted on a ready System, or None.

    ``systems.list`` does not populate ``active_run.state``, so confirm each ready System's
    booted Run via ``systems.get`` (which does carry it).
    """
    systems = await _call(client, "systems.list", {}, schemas)
    for item in systems.get("items", []):
        if (item.get("data") or {}).get("state") != "ready" and item.get("status") != "ready":
            continue
        detail = await _call(client, "systems.get", {"system_id": item["object_id"]}, schemas)
        active = (detail.get("data") or {}).get("active_run") or {}
        if active.get("id") and active.get("state") in {"booted", "succeeded", "ready"}:
            return str(active["id"])
    return None


async def _provision_boot_run(
    client: LiveStackClient, schemas: dict[str, dict[str, Any]], *, project: str
) -> str:
    """Full lifecycle: investigation -> allocation -> provision -> build/install/boot -> run_id."""
    resources = await _call(client, "resources.list", {}, schemas)
    resource_id = (resources["items"][0]["object_id"]) if resources.get("items") else None
    if not resource_id:
        raise RuntimeError("no registered local-libvirt resource; run setup-local-libvirt.sh")
    inv = await _call(
        client, "investigations.open", {"project": project, "title": "live-debug"}, schemas
    )
    inv_id = inv["object_id"]
    print(f"  investigation {inv_id}", file=sys.stderr)
    await _call(
        client,
        "allocations.request",
        {
            "project": project,
            "request": {
                "shape": "medium",
                "window": 24,
                "resource": {"mode": "id", "resource_id": resource_id},
            },
        },
        schemas,
    )
    allocs = await _call(client, "allocations.list", {"project": project}, schemas)
    alloc_id = allocs["items"][0]["object_id"]
    profile = {
        "schema_version": 1,
        "arch": "x86_64",
        "boot_method": "direct-kernel",
        "kernel_source_ref": "linux-live-debug",
        "provider": {
            "local-libvirt": {
                "rootfs": {
                    "kind": "catalog",
                    "provider": "local-libvirt",
                    "name": "fedora-kdive-ready-44",
                },
                "debug": {"gdbstub": True},
            }
        },
    }
    sysenv = await _call(
        client, "systems.provision", {"allocation_id": alloc_id, "profile": profile}, schemas
    )
    system_id = sysenv["data"]["system_id"]
    await _poll(
        client,
        "systems.get",
        {"system_id": system_id},
        schemas,
        done={"ready", "failed", "cordoned"},
        timeout_sec=600,
        label="provision",
    )
    print(f"  system {system_id} (alloc {alloc_id})", file=sys.stderr)
    run = await _call(
        client,
        "runs.create",
        {
            "request": {
                "investigation_id": inv_id,
                "system_id": system_id,
                "build_profile": {
                    "schema_version": 1,
                    "source": "server",
                    "kernel_source_ref": "linux-live-debug",
                    "build_host": "worker-local",
                },
            }
        },
        schemas,
    )
    run_id = run["object_id"]
    for step, terminal in (("runs.build", 3000), ("runs.install", 600), ("runs.boot", 600)):
        kind = step.split(".")[1]
        await _call(client, step, {"run_id": run_id}, schemas)
        await _wait_job(client, schemas, kind=kind, timeout_sec=terminal)
    print(f"  run {run_id} booted", file=sys.stderr)
    return str(run_id)


async def _stopped(args: argparse.Namespace) -> int:
    """Reach a stopped gdbstub session and print its id (and teardown handles)."""
    async with LiveStackClient.over_http(BASE_URL, _token(args.project)) as client:
        schemas = await _input_schemas(client)
        run_id = (await _find_booted_run(client, schemas)) if args.reuse else None
        if run_id:
            print(f"  reusing booted run {run_id}", file=sys.stderr)
        else:
            run_id = await _provision_boot_run(client, schemas, project=args.project)
        await _call(
            client, "debug.start_session", {"run_id": run_id, "transport": "gdbstub"}, schemas
        )
        sessions = await _call(client, "debug.list_sessions", {}, schemas)
        session_id = sessions["items"][0]["object_id"]
        await _call(
            client,
            "debug.set_breakpoint",
            {"session_id": session_id, "location": args.symbol},
            schemas,
        )
        cont = await _call(
            client, "debug.continue", {"session_id": session_id, "timeout_sec": 30}, schemas
        )
        reason = (cont.get("data") or {}).get("reason")
        print(f"  stopped: {reason} at {args.symbol}", file=sys.stderr)
        print(f"SESSION_ID={session_id}")
        print(f"RUN_ID={run_id}")
        return 0


# --- simple commands -----------------------------------------------------------------------


async def _cmd_call(args: argparse.Namespace) -> int:
    payload = json.loads(args.args) if args.args else {}
    async with LiveStackClient.over_http(BASE_URL, _token(args.project)) as client:
        schemas = await _input_schemas(client)
        print(json.dumps(await _call(client, args.tool, payload, schemas), indent=2, default=str))
    return 0


async def _cmd_tools(args: argparse.Namespace) -> int:
    async with LiveStackClient.over_http(BASE_URL, _token(args.project)) as client:
        names = sorted(await client.list_tools())
    for name in names:
        if not args.substr or args.substr in name:
            print(name)
    return 0


async def _cmd_schema(args: argparse.Namespace) -> int:
    async with LiveStackClient.over_http(BASE_URL, _token(args.project)) as client:
        schemas = await _input_schemas(client)
    for tool in args.tools:
        print(f"### {tool}")
        print(json.dumps(schemas.get(tool, {}).get("properties", {}), indent=1, default=str))
    return 0


async def _cmd_teardown(args: argparse.Namespace) -> int:
    async with LiveStackClient.over_http(BASE_URL, _token(args.project)) as client:
        schemas = await _input_schemas(client)
        env = await _call(client, "systems.teardown", {"system_id": args.system_id}, schemas)
        print(json.dumps(env, indent=2, default=str))
    return 0


def _cmd_transcript(args: argparse.Namespace) -> int:
    """Pretty-print the per-session gdb/MI transcript -- the raw command/records gdb returned."""
    path = DEBUG_DIR / f"{args.session_id}.jsonl"
    if not path.is_file():
        print(f"no transcript at {path}", file=sys.stderr)
        return 1
    for line in path.read_text().splitlines():
        record = json.loads(line)
        command = record.get("command")
        print(f"\n$ {command}")
        for entry in record.get("records", []):
            print(
                f"    {entry.get('type')}/{entry.get('message')}: "
                f"{json.dumps(entry.get('payload'), default=str)[:600]}"
            )
    return 0


def _server_pids() -> list[int]:
    """PIDs of the actual ``kdive server`` daemon (not the bash launcher wrapper)."""
    out = subprocess.run(  # noqa: S603 - fixed pgrep argv  # nosec B603
        [_required_executable("pgrep"), "-af", "kdive server"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids: list[int] = []
    for line in out.stdout.splitlines():
        pid, _, cmd = line.partition(" ")
        if cmd.rstrip().endswith("-m kdive server") and ".venv/bin/python" in cmd:
            pids.append(int(pid))
    return pids


def _cmd_reload(args: argparse.Namespace) -> int:
    """Stop only the server daemon and relaunch it so a code change takes effect."""
    del args
    py = REPO_ROOT / ".venv/bin/python"
    log_dir = REPO_ROOT / ".live-stack-logs"
    for pid in _server_pids():
        print(f"  stopping server {pid}", file=sys.stderr)
        subprocess.run(  # noqa: S603 - fixed kill argv; pid parsed as int  # nosec B603
            [_required_executable("kill"), str(pid)], check=False
        )
    for _ in range(40):
        if not _server_pids():
            break
        time.sleep(0.5)
    log_dir.mkdir(exist_ok=True)
    launch = (
        f"cd {REPO_ROOT} && source scripts/live-stack/env.sh "
        f"&& setsid nohup {py} -m kdive server >>{log_dir}/server.log 2>&1 </dev/null &"
    )
    subprocess.run(  # noqa: S603 - dev reload uses fixed bash argv and script  # nosec B603
        [_required_executable("bash"), "-c", launch], check=True
    )
    for _ in range(40):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(BASE_URL, timeout=2)  # noqa: S310 - localhost  # nosec B310
        except urllib.error.HTTPError:
            print(f"  server up @ {BASE_URL}", file=sys.stderr)
            return 0
        except urllib.error.URLError, ConnectionError, OSError:
            continue
    print("  server did not answer in time; check .live-stack-logs/server.log", file=sys.stderr)
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--project", default="demo", help="project for token/onboarding (default demo)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    stopped = sub.add_parser("stopped", help="drive to a stopped gdbstub session")
    stopped.add_argument(
        "--reuse", action="store_true", help="reuse an already-booted Run if present"
    )
    stopped.add_argument(
        "--symbol", default=DEFAULT_BREAK_SYMBOL, help="breakpoint symbol to stop at"
    )

    call = sub.add_parser("call", help="call one tool (auto-wraps the `request` arg)")
    call.add_argument("tool")
    call.add_argument("args", nargs="?", default="{}", help="JSON object of arguments")

    tools = sub.add_parser("tools", help="list tool names")
    tools.add_argument("substr", nargs="?", help="only names containing this substring")

    schema = sub.add_parser("schema", help="dump tool input schemas")
    schema.add_argument("tools", nargs="+")

    transcript = sub.add_parser("transcript", help="print a session's gdb/MI transcript")
    transcript.add_argument("session_id")

    teardown = sub.add_parser("teardown", help="tear down a System (releases its allocation)")
    teardown.add_argument("system_id")

    sub.add_parser("reload", help="restart only the server daemon (load a code change)")
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    if args.command == "transcript":
        return _cmd_transcript(args)
    if args.command == "reload":
        return _cmd_reload(args)
    handlers = {
        "stopped": _stopped,
        "call": _cmd_call,
        "tools": _cmd_tools,
        "schema": _cmd_schema,
        "teardown": _cmd_teardown,
    }
    return asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
