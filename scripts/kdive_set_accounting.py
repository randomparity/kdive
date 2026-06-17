"""Onboard a project through the audited accounting admin tools over MCP.

Calls ``accounting.set_quota`` then ``accounting.set_budget`` (and reads back
``accounting.usage_project``) against a running KDIVE server's MCP endpoint, using a
bearer token that carries the project ``admin`` role. This is the production-style,
audited alternative to ``seed-demo``'s raw INSERTs (see
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
        ("accounting.usage_project", {"project": ns.project}),
    ]


async def run(ns: argparse.Namespace) -> int:
    """Execute the onboarding calls; return a process exit code."""
    if not ns.token:
        print("error: no token (set KDIVE_TOKEN or pass --token)", file=sys.stderr)
        return 2
    transport = StreamableHttpTransport(
        url=ns.base, headers={"Authorization": f"Bearer {ns.token}"}
    )
    rc = 0
    async with Client(transport) as client:
        for name, arguments in build_calls(ns):
            result = await client.call_tool(name, arguments, raise_on_error=False)
            if getattr(result, "is_error", False):
                print(f"error: tool {name} failed", file=sys.stderr)
                rc = 1
            print(json.dumps(result.structured_content, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse())))
