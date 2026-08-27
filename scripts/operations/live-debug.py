"""Interactive driver for the gdb-MI debug tier against a live local-libvirt stack.

Collapses the build -> boot -> attach -> stopped loop you re-walk when developing or
testing new ``debug.*`` tools into single commands. A run-local dev tool (mirrors
``scripts/coverage_campaign/drive.py``); not wired into CI.

The stack must already be up (``scripts/live-stack/up.sh``). Examples::

    # one command to a stopped gdbstub session (reuses a booted Run if one exists):
    uv run python scripts/operations/live-debug.py stopped --reuse
    # call any tool (auto-wraps the `request` arg per the tool's own schema):
    uv run python scripts/operations/live-debug.py call debug.backtrace '{"session_id": "..."}'
    # raw gdb/MI transcript -- ground truth when a parser disagrees with gdb:
    uv run python scripts/operations/live-debug.py transcript <session_id>
    # restart ONLY the server process to load a code change (keeps the booted VM):
    uv run python scripts/operations/live-debug.py reload
    # release the System + its allocation when done:
    uv run python scripts/operations/live-debug.py teardown <system_id>
    uv run python scripts/operations/live-debug.py tools [substr]
    uv run python scripts/operations/live-debug.py schema <tool> [...]

Auth: ``KDIVE_TOKEN`` if set, else an admin token is minted for ``--project`` (default
``demo``) via the bundled mock OIDC issuer. Base URL: ``KDIVE_STACK_BASE_URL`` or
``http://127.0.0.1:8000/mcp``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess  # noqa: S404 - fixed dev-tool argv, no shell except reload  # nosec B404
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from kdive.mcp.dev_harness import LiveStackClient
from kdive.mcp.schema.tool_index import NAMESPACE_TOC
from scripts.operations.live_debug_build import (
    _find_booted_run,
    _provision_boot_run,
    _required_executable,
)
from scripts.operations.live_debug_transport import (
    _SEARCH_LIMIT,
    _as_dict,
    _call,
    _SchemaResolver,
    _token,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_URL = os.environ.get("KDIVE_STACK_BASE_URL", "http://127.0.0.1:8000/mcp")
DEBUG_DIR = Path(os.environ.get("KDIVE_DEBUG_DIR", "/var/lib/kdive/debug"))
# The built kernel tree the combined upload tar is cut from (bzImage + its module tree). Defaults
# to the same warm tree scripts/live-stack uses; must already be built (arch/x86/boot/bzImage).
DEFAULT_BREAK_SYMBOL = "schedule"  # hot enough that a single -exec-continue stops at once
# The stepping proof needs a promptly-returning, same-stack function so `finish` returns within
# the wait cap. `schedule` will not do: it yields the CPU and only "returns" when this task is
# rescheduled, and single-stepping across `__switch_to` confuses gdb. A hot VFS-read helper
# returns to its caller on the same stack.
STEP_DEFAULT_SYMBOL = "vfs_read"


async def _stopped(args: argparse.Namespace) -> int:
    """Reach a stopped gdbstub session and print its id (and teardown handles)."""
    async with LiveStackClient.over_http(BASE_URL, _token(args.project)) as client:
        schemas = _SchemaResolver()
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


async def _rip(client: LiveStackClient, schemas: _SchemaResolver, session_id: str) -> str | None:
    resp = await _call(
        client, "debug.read_registers", {"session_id": session_id, "registers": ["rip"]}, schemas
    )
    return (resp.get("data") or {}).get("rip")


async def _step(args: argparse.Namespace) -> int:
    """Prove every debug.advance mode (#1584) at a returnable frame on a booted kernel."""
    async with LiveStackClient.over_http(BASE_URL, _token(args.project)) as client:
        schemas = _SchemaResolver()
        run_id = (await _find_booted_run(client, schemas)) if args.reuse else None
        if not run_id:
            run_id = await _provision_boot_run(client, schemas, project=args.project)
        await _call(
            client, "debug.start_session", {"run_id": run_id, "transport": "gdbstub"}, schemas
        )
        sessions = await _call(client, "debug.list_sessions", {}, schemas)
        session_id = sessions["items"][0]["object_id"]
        bp = await _call(
            client,
            "debug.set_breakpoint",
            {"session_id": session_id, "location": args.symbol},
            schemas,
        )
        await _call(
            client, "debug.continue", {"session_id": session_id, "timeout_sec": 30}, schemas
        )
        # A degraded attach (no published vmlinux) answers every op with an error envelope
        # instead of a breakpoint number; say so here rather than dying on a KeyError below.
        number = (bp.get("data") or {}).get("number")
        if number is None:
            print(f"  FAIL set_breakpoint returned no number: {bp}", file=sys.stderr)
            return 1
        print(f"  stopped at {args.symbol}", file=sys.stderr)
        # Clear the breakpoint so it does not re-fire mid-walk and mask a step's own stop.
        await _call(
            client,
            "debug.clear_breakpoint",
            {"session_id": session_id, "number": number},
            schemas,
        )
        # instruction/over/into must each advance rip (deterministic on a returnable frame).
        for mode in ("instruction", "over", "into"):
            before = await _rip(client, schemas, session_id)
            resp = await _call(
                client,
                "debug.advance",
                {"session_id": session_id, "mode": mode, "timeout_sec": 15},
                schemas,
            )
            after = await _rip(client, schemas, session_id)
            if (resp.get("data") or {}).get("timed_out") or before == after:
                print(f"  FAIL mode={mode}: rip {before} -> {after} data={resp.get('data')}")
                return 1
            print(f"  mode={mode}: rip {before} -> {after}")
        # mode=out must return to the caller within the wait cap (timed_out=False).
        out = await _call(
            client,
            "debug.advance",
            {"session_id": session_id, "mode": "out", "timeout_sec": 30},
            schemas,
        )
        if (out.get("data") or {}).get("timed_out") is not False:
            print(f"  FAIL mode=out timed out: {out.get('data')}")
            return 1
        out_reason = (out.get("data") or {}).get("reason")
        print(f"  mode=out: returned to caller (timed_out=False), reason={out_reason}")
        print("STEP_PROOF=ok")
        return 0


# --- simple commands -----------------------------------------------------------------------


async def _cmd_call(args: argparse.Namespace) -> int:
    payload = json.loads(args.args) if args.args else {}
    async with LiveStackClient.over_http(BASE_URL, _token(args.project)) as client:
        schemas = _SchemaResolver()
        print(json.dumps(await _call(client, args.tool, payload, schemas), indent=2, default=str))
    return 0


async def _namespace_tools(client: LiveStackClient, namespace: str) -> list[str]:
    """Every tool name in ``namespace``, via ``tools.search``'s namespace-browse mode."""
    envelope = _as_dict(
        await client.call_tool("tools.search", namespace=namespace, limit=_SEARCH_LIMIT)
    )
    data = envelope.get("data") or {}
    if data.get("truncated"):
        print(
            f"  [warn] namespace {namespace!r} holds more than {_SEARCH_LIMIT} tools; "
            "this listing is truncated",
            file=sys.stderr,
        )
    return [
        str(match["name"])
        for match in data.get("matches") or []
        if isinstance(match, dict) and match.get("name")
    ]


async def _cmd_tools(args: argparse.Namespace) -> int:
    """List tool names, enumerating every live namespace through ``tools.search``.

    ``list_tools`` would show only the nine core tools under the default surface, which would
    make this discovery command hide exactly the tools it exists to find. Browsing each namespace
    in ``NAMESPACE_TOC`` reaches the whole catalog the caller's roles allow.
    """
    names: set[str] = set()
    async with LiveStackClient.over_http(BASE_URL, _token(args.project)) as client:
        for namespace in sorted(NAMESPACE_TOC):
            names.update(await _namespace_tools(client, namespace))
    for name in sorted(names):
        if not args.substr or args.substr in name:
            print(name)
    return 0


async def _cmd_schema(args: argparse.Namespace) -> int:
    async with LiveStackClient.over_http(BASE_URL, _token(args.project)) as client:
        schemas = _SchemaResolver()
        for tool in args.tools:
            schema = await schemas.schema(client, tool)
            print(f"### {tool}")
            print(json.dumps(schema.get("properties", {}), indent=1, default=str))
    return 0


async def _cmd_teardown(args: argparse.Namespace) -> int:
    async with LiveStackClient.over_http(BASE_URL, _token(args.project)) as client:
        schemas = _SchemaResolver()
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

    step = sub.add_parser("step", help="prove every debug.advance mode (#1584)")
    step.add_argument("--reuse", action="store_true", help="reuse an already-booted Run if present")
    step.add_argument(
        "--symbol",
        default=STEP_DEFAULT_SYMBOL,
        help="returnable, same-stack breakpoint symbol to step from (not a scheduler function)",
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
        "step": _step,
        "call": _cmd_call,
        "tools": _cmd_tools,
        "schema": _cmd_schema,
        "teardown": _cmd_teardown,
    }
    return asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
