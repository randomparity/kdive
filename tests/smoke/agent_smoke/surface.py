"""Adapt a built FastMCP app to the walker's normalized :class:`Surface` (#1370, ADR-0411).

The walker speaks in normalized strings (tool names, resource URIs and bodies, prompt names
and rendered text); this adapter maps those onto the served MCP surface a real agent reaches:
``list_tools`` / ``list_resources`` / ``read_resource`` / ``list_prompts`` / ``render_prompt``.

A read or render that *raises* is itself a stall — the agent asked the surface for something
it could not deliver — so the adapter converts a failure into an empty string, which the
walker records as a dead end. This is deliberate surfacing of the failure as a walk result,
not silent swallowing.
"""

from __future__ import annotations

from fastmcp import FastMCP


class AppSurface:
    """The walker's :class:`~tests.smoke.agent_smoke.walker.Surface` over a built app."""

    def __init__(self, app: FastMCP) -> None:
        self._app = app

    async def tool_names(self) -> frozenset[str]:
        return frozenset(tool.name for tool in await self._app.list_tools())

    async def resource_uris(self) -> frozenset[str]:
        return frozenset(str(resource.uri) for resource in await self._app.list_resources())

    async def read(self, uri: str) -> str:
        try:
            result = await self._app.read_resource(uri)
        except Exception:  # a raised read is a dead end the walk must record
            return ""
        content = result.contents[0].content
        return content if isinstance(content, str) else ""

    async def prompt_names(self) -> frozenset[str]:
        return frozenset(prompt.name for prompt in await self._app.list_prompts())

    async def render(self, name: str) -> str:
        try:
            result = await self._app.render_prompt(name, {})
        except Exception:  # a raised render is a dead end the walk must record
            return ""
        text = getattr(result.messages[0].content, "text", "")
        return text if isinstance(text, str) else ""
