"""Authentication, schema resolution, calls, and polling for live-debug."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

from kdive.mcp.dev_harness import LiveStackClient, mint_token, oidc_issuer_from_env
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.gateway import _SEARCH_LIMIT_MAX

_POLL_INTERVAL_SEC = 5.0
_SEARCH_LIMIT = _SEARCH_LIMIT_MAX


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
    """Map every *advertised* tool name to its JSON input schema.

    Only what the connection's exposure profile lists, which is why :class:`_SchemaResolver`
    has a second source for the rest.
    """
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


async def _search_schema(client: LiveStackClient, tool: str) -> dict[str, Any]:
    """``tool``'s input schema from ``tools.search``, or ``{}`` when no match carries that name.

    An empty answer is not silent: it means the tool call below will send whatever shape the
    caller passed, so the reason (a typo, a tool the caller's roles hide, a clipped result set)
    is worth naming here rather than leaving the user to decode a server-side validation error.
    """
    envelope = _as_dict(await client.call_tool("tools.search", query=tool, limit=_SEARCH_LIMIT))
    data = envelope.get("data") or {}
    for match in data.get("matches") or []:
        # Ranking is lexical over name, description, keywords and schema text, so an unrelated
        # tool can outrank the exact name; only an exact name match is this tool's own schema.
        if isinstance(match, dict) and match.get("name") == tool:
            return dict(match.get("input_schema") or {})
    clipped = " (results were clipped at the search limit)" if data.get("truncated") else ""
    print(
        f"  [warn] no input schema for {tool!r}{clipped}: unknown tool, or one your roles hide. "
        "Its arguments will be sent exactly as given.",
        file=sys.stderr,
    )
    return {}


class _SchemaResolver:
    """Resolve each tool's JSON input schema, whatever the connection's exposure profile lists.

    ``list_tools`` is clipped to the nine core tools for the agent profile this script's own
    token lands on (the gateway is on by default), so most names are simply absent from it even
    though they stay callable directly — exposure is advisory, not a security control. Treating
    an absent name as "no schema" makes every single-``request`` tool get called with flat kwargs,
    which the server rejects, so a miss falls back to ``tools.search``: it is RBAC-filtered rather
    than profile-clipped and each match carries the same full ``input_schema``.

    Every answer is cached, including a miss. The pollers re-enter :func:`_call` for the same tool
    every few seconds, so an uncached miss would issue one ``tools.search`` per poll tick. The
    cache is written after the await, so it dedupes only a serial caller — which is all this
    single-threaded driver has.
    """

    def __init__(self) -> None:
        self._schemas: dict[str, dict[str, Any]] = {}
        self._listed = False

    async def schema(self, client: LiveStackClient, tool: str) -> dict[str, Any]:
        """The input schema for ``tool``, or ``{}`` if neither surface advertises one."""
        if not self._listed:
            self._schemas.update(await _input_schemas(client))
            self._listed = True
        if tool not in self._schemas:
            self._schemas[tool] = await _search_schema(client, tool)
        return self._schemas[tool]


async def _call(
    client: LiveStackClient, tool: str, args: dict[str, Any], schemas: _SchemaResolver
) -> dict[str, Any]:
    resolved = _wrap_request(await schemas.schema(client, tool), args)
    return _as_dict(await client.call_tool(tool, **resolved))


# --- polling -------------------------------------------------------------------------------


async def _poll(
    client: LiveStackClient,
    tool: str,
    args: dict[str, Any],
    schemas: _SchemaResolver,
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
    schemas: _SchemaResolver,
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
        "jobs.wait",
        {"job_id": job_id, "timeout_s": 0},
        schemas,
        done=_JOB_DONE,
        timeout_sec=timeout_sec,
        label=kind,
    )
    data = final.get("data") or {}
    status = data.get("status") or data.get("state") or final.get("status")
    if status not in {"succeeded", "completed"}:
        raise RuntimeError(f"{kind} job {job_id} ended {status!r}: {final.get('detail')}")


# --- external build: combined kernel tar + upload ------------------------------------------
