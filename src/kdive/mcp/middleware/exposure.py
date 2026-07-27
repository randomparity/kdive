"""Per-connection tool exposure filtering middleware."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools import Tool
from opentelemetry import metrics

from kdive.domain.catalog.resources import ResourceKind
from kdive.mcp.exposure import (
    CORE_TOOLS,
    ToolExposureProfile,
    gateway_enabled,
    resolve_exposure_profile,
    visible_tool_names,
)
from kdive.mcp.middleware.shared import request_context
from kdive.mcp.schema.tool_projection import project_listed_tool
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.errors import AuthError

_log = logging.getLogger(__name__)

_PROJECTION_FAILURES = metrics.get_meter("kdive.mcp").create_counter(
    "kdive_mcp_provider_schema_projection_failures",
    description="provider-schema projection fell open to the full schema (ADR-0269)",
)
_EXPOSURE_FAILOPEN = metrics.get_meter("kdive.mcp").create_counter(
    "kdive_mcp_tool_exposure_fail_open",
    description="tool-exposure filter fell open to the full catalog (ADR-0269)",
)
_PROFILE_SELECTIONS = metrics.get_meter("kdive.mcp").create_counter(
    "kdive_mcp_tool_exposure_profile_selections",
    description="tool-exposure profile selections by resolved profile and source (ADR-0456)",
)


def _record_profile(profile: ToolExposureProfile, source: str) -> None:
    _PROFILE_SELECTIONS.add(1, {"profile": profile.value, "source": source})


def _resolve_profile_or_agent(ctx: Any) -> ToolExposureProfile:
    """Resolve the exposure profile, falling back to agent on resolver defects."""
    try:
        profile = resolve_exposure_profile(ctx)
    except Exception:
        profile = ToolExposureProfile.AGENT_GATEWAY
        _record_profile(profile, "fallback")
        _log.warning(
            "tool-exposure profile resolution failed; using agent profile",
            exc_info=True,
        )
        return profile
    source = "oidc_client_id" if profile is ToolExposureProfile.OPERATOR_DIRECT else "default"
    _record_profile(profile, source)
    return profile


def _narrow_or_passthrough(tool: Tool, kinds: frozenset[ResourceKind] | None) -> Tool:
    """Project ``tool``'s schema to ``kinds``, or return it unprojected on any failure.

    Args:
        tool: The tool to project.
        kinds: Composed provider kinds, or ``None`` when the resolver failed.

    Returns:
        A projected copy of ``tool``, or the original on failure or missing kinds.
    """
    if kinds is None:
        return tool
    try:
        return project_listed_tool(tool, kinds)
    except Exception:
        _PROJECTION_FAILURES.add(1)
        _log.warning(
            "provider-schema projection failed for %s; advertising full schema",
            tool.name,
            exc_info=True,
        )
        return tool


class ToolExposureMiddleware(Middleware):
    """Filter ``list_tools`` to tools the connection's grants could invoke.

    Also narrows each affected tool's ``inputSchema`` to the composed provider kinds
    (ADR-0269). The projection is fail-open: a schema narrowing error advertises the full
    schema, increments a counter, and logs a warning so a silent revert is observable.
    """

    def __init__(self, resolver: ProviderResolver) -> None:
        self._resolver = resolver

    async def on_list_tools(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Sequence[Tool]:
        """Return only the advertised tools the in-flight connection may invoke.

        Narrows each narrowed tool's ``inputSchema`` to the composed provider kinds after
        the RBAC filter has reduced the catalog to the visible set.
        """
        tools: Sequence[Tool] = await call_next(context)
        # Stage 1: context / RBAC / gateway-filter → compute visible set.
        try:
            ctx = request_context()
            visible = visible_tool_names(ctx, (tool.name for tool in tools))
            profile = _resolve_profile_or_agent(ctx)
            if gateway_enabled() and profile is ToolExposureProfile.AGENT_GATEWAY:
                visible &= CORE_TOOLS
        except AuthError:
            _log.debug("no verified token in on_list_tools; advertising the full catalog")
            return tools
        except Exception:
            _EXPOSURE_FAILOPEN.add(1)
            _log.warning("tool-exposure filter failed; advertising the full catalog", exc_info=True)
            return tools
        # Stage 2: provider-schema narrowing → compute composed kinds.
        kinds: frozenset[ResourceKind] | None = None
        try:
            kinds = self._resolver.registered_kinds()
        except Exception:
            _PROJECTION_FAILURES.add(1)
            _log.warning(
                "registered_kinds() failed; advertising visible tools unprojected", exc_info=True
            )
        return [_narrow_or_passthrough(tool, kinds) for tool in tools if tool.name in visible]
