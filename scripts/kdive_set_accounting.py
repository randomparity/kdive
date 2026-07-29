"""Onboard a project through the audited accounting admin tools over MCP.

Calls ``accounting.set_quota`` then ``accounting.set_budget`` (and reads back
``accounting.usage``) against a running KDIVE server's MCP endpoint, using a
bearer token that carries the project ``admin`` role. This is the production-style,
audited alternative to ``seed-project``'s raw INSERTs (see
``docs/operating/project-onboarding.md``).

DEMO/operator helper. The bundled mock OIDC issuer mints a valid token for any caller, so
never point this at a real deployment; production supplies its own token via ``KDIVE_TOKEN``.
The ``--base`` URL must end in ``/mcp``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the accounting onboarding call."""
    p = argparse.ArgumentParser(prog="kdive_set_accounting.py")
    p.add_argument("--base", required=True, help="server MCP endpoint, must end in /mcp")
    p.add_argument("--project", default="demo")
    p.add_argument("--limit-kcu", dest="limit_kcu", default="1000000")
    p.add_argument("--max-concurrent-allocations", dest="max_alloc", type=int, default=4)
    p.add_argument("--max-concurrent-systems", dest="max_sys", type=int, default=4)
    p.add_argument("--max-pending-allocations", dest="max_pending", type=int, default=0)
    p.add_argument("--token", default=os.environ.get("KDIVE_TOKEN"))
    return p.parse_args(argv)


def build_calls(ns: argparse.Namespace) -> list[tuple[str, dict[str, object]]]:
    """Return the ordered (tool, arguments) pairs for onboarding ``ns.project``."""
    return [
        (
            "accounting.set_quota",
            {
                "project": ns.project,
                "max_concurrent_allocations": ns.max_alloc,
                "max_concurrent_systems": ns.max_sys,
                "max_pending_allocations": ns.max_pending,
            },
        ),
        ("accounting.set_budget", {"project": ns.project, "limit_kcu": ns.limit_kcu}),
        ("accounting.usage", {"target": {"kind": "project", "project": ns.project}}),
    ]


def _failed(result: object) -> bool:
    """True when the call failed, in either shape a KDIVE tool can signal one.

    ``is_error`` is the transport flag, set when the tool *raised*. A tool that denies or fails
    by *returning* a failure envelope leaves it False and reports ``error_category`` instead
    (ADR-0089), which is the shape an authorization denial takes since ADR-0486 — before that
    it raised, so this helper's ``is_error`` check happened to cover it.
    """
    if getattr(result, "is_error", False):
        return True
    structured = getattr(result, "structured_content", None)
    return isinstance(structured, dict) and structured.get("error_category") is not None


async def run(ns: argparse.Namespace) -> int:
    """Execute the onboarding calls; return a process exit code."""
    if not ns.token:
        print("error: no token (set KDIVE_TOKEN or pass --token)", file=sys.stderr)
        return 2
    transport = StreamableHttpTransport(
        url=ns.base, headers={"Authorization": f"Bearer {ns.token}"}
    )
    async with Client(transport) as client:
        for name, arguments in build_calls(ns):
            result = await client.call_tool(name, arguments, raise_on_error=False)
            if _failed(result):
                print(f"error: tool {name} failed", file=sys.stderr)
                print(json.dumps(result.structured_content, default=str), file=sys.stderr)
                return 1
            print(json.dumps(result.structured_content, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse())))
