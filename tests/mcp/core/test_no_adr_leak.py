"""CI guard: no internal ``ADR-NNNN`` citation may reach the agent-facing MCP surface.

ADRs (``docs/adr/``) are internal design records. FastMCP renders each tool function's
docstring as the tool ``description`` and each Pydantic ``Field(description=...)`` / model /
enum docstring as a schema ``description`` the client sees, so an ``ADR-NNNN`` written as if
it were an internal note in fact leaks into the production agent contract.

This guard builds the live app and fails if ``ADR-\\d+`` appears in any agent-rendered
string: the server ``instructions``; any string in a registered tool's rendered MCP form
(description, input/output schema, annotations, meta); any registered resource's rendered
form; or any prompt's rendered form. It also asserts no ``resource://kdive/adr/*`` is served.

The walk is over **every string leaf** (no key allowlist), so a ref hiding in an
``examples``/``const``/enum value is caught too. A vacuity canary proves the matcher and the
walk are not silently matching nothing.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from typing import Any, cast

from fastmcp.prompts.base import Prompt
from fastmcp.resources.base import Resource
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.tools.base import Tool
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.app import build_app
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair

_ADR = re.compile(r"ADR-\d+")
_ADR_RESOURCE_PREFIX = "resource://kdive/adr/"


def _strings(obj: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(json-path, value)`` for every string leaf in a nested dict/list/str."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from _strings(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from _strings(value, f"{path}[{index}]")


def _adr_offenders(obj: Any, prefix: str) -> list[str]:
    """Every ``prefix<path> :: <value>`` whose string leaf carries an ``ADR-NNNN`` ref."""
    return [f"{prefix}{path} :: {value}" for path, value in _strings(obj) if _ADR.search(value)]


def _build_app() -> Any:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    keypair = make_keypair()
    verifier = JWTVerifier(public_key=keypair.public_key, issuer=ISSUER, audience=AUDIENCE)
    return build_app(pool, verifier=verifier, secret_registry=SecretRegistry())


_APP = _build_app()
_TOOLS = cast(list[Tool], asyncio.run(_APP.list_tools()))
_RESOURCES = cast(list[Resource], asyncio.run(_APP.list_resources()))
_PROMPTS = cast(list[Prompt], asyncio.run(_APP.list_prompts()))


def test_no_adr_refs_in_tool_surface() -> None:
    offenders: list[str] = []
    for tool in _TOOLS:
        dumped = tool.to_mcp_tool().model_dump(mode="json")
        offenders.extend(_adr_offenders(dumped, tool.name))
    assert not offenders, "ADR refs leak into the agent-facing tool surface:\n" + "\n".join(
        offenders
    )


def test_no_adr_refs_in_server_instructions() -> None:
    instructions = _APP.instructions or ""
    leaked = _ADR.findall(instructions)
    assert not leaked, f"ADR refs leak into the server instructions: {leaked}"


def test_no_adr_refs_in_registered_resources() -> None:
    offenders: list[str] = []
    served_adr_uris: list[str] = []
    for resource in _RESOURCES:
        mcp_resource = resource.to_mcp_resource()
        dumped = mcp_resource.model_dump(mode="json")
        offenders.extend(_adr_offenders(dumped, str(mcp_resource.uri)))
        if str(mcp_resource.uri).startswith(_ADR_RESOURCE_PREFIX):
            served_adr_uris.append(str(mcp_resource.uri))
    assert not served_adr_uris, f"ADRs served as MCP resources: {served_adr_uris}"
    assert not offenders, "ADR refs leak into registered resource metadata:\n" + "\n".join(
        offenders
    )


def test_no_adr_refs_in_prompts() -> None:
    offenders: list[str] = []
    for prompt in _PROMPTS:
        dumped = prompt.to_mcp_prompt().model_dump(mode="json")
        offenders.extend(_adr_offenders(dumped, prompt.name))
    assert not offenders, "ADR refs leak into the agent-facing prompt surface:\n" + "\n".join(
        offenders
    )


def test_adr_matcher_is_not_vacuous() -> None:
    # Canary: the matcher flags a known-bad string, and `_strings` actually descends a
    # nested dict/list — so a broken walk cannot make the surface guards pass by yielding
    # nothing.
    assert _ADR.search("see ADR-0019")
    nested = {"a": {"b": ["clean", "see ADR-0001"]}}
    leaves = [value for _, value in _strings(nested)]
    assert "see ADR-0001" in leaves
    assert _adr_offenders(nested, "probe") == ["probe.a.b[1] :: see ADR-0001"]
