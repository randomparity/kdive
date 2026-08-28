"""The tool gateway: tools.invoke (dispatcher) + tools.search (discovery) (ADR-0268, #866).

The inner-call denial path (``authorization_denied`` via the denial-audit middleware) is
ADR-0148. Agent-rendered docstrings here carry no ADR citation (ADR-0270).
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Annotated, Any, cast

from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.tools.base import Tool, ToolResult
from pydantic import Field, ValidationError

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.auth import current_context
from kdive.mcp.exposure import required_scopes, tool_visible
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.schema_advertising import registered_tools
from kdive.mcp.schema.tool_index import TOOL_KEYWORDS, retired_names_for
from kdive.mcp.schema.tool_projection import project_listed_tool
from kdive.mcp.tools import _docmeta
from kdive.providers.core.resolver import ProviderResolver
from kdive.serialization import JsonValue

_log = logging.getLogger(__name__)

# Hard cap on search results — prevents one broad query from re-emitting the full catalog.
_SEARCH_LIMIT_MAX = 50
_SCHEMA_TERM_LIMIT = 200
_SCHEMA_DEPTH_LIMIT = 8


class SearchDetail(StrEnum):
    """How much per-match metadata ``tools.search`` returns (ADR-0472)."""

    SUMMARY = "summary"
    FULL = "full"


class NamespaceStatus(StrEnum):
    """What a ``namespace`` argument named in the live registry (ADR-0472)."""

    OK = "ok"
    UNAUTHORIZED = "unauthorized"
    UNKNOWN = "unknown"


_SCHEMA_TEXT_KEYS = frozenset(
    {
        "description",
        "title",
        "const",
        "default",
        "format",
        "pattern",
        "propertyName",
        "type",
    }
)


def _append_schema_terms(value: object, terms: list[str], *, depth: int = 0) -> None:
    """Collect bounded searchable text from a JSON Schema-like object."""
    if len(terms) >= _SCHEMA_TERM_LIMIT or depth > _SCHEMA_DEPTH_LIMIT:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if len(terms) >= _SCHEMA_TERM_LIMIT:
                return
            if key == "properties" and isinstance(child, dict):
                for prop_name, prop_schema in child.items():
                    if len(terms) >= _SCHEMA_TERM_LIMIT:
                        return
                    terms.append(str(prop_name))
                    _append_schema_terms(prop_schema, terms, depth=depth + 1)
                continue
            if key in {"required", "enum"} and isinstance(child, list):
                terms.extend(str(item) for item in child[: _SCHEMA_TERM_LIMIT - len(terms)])
                continue
            if key in {"$defs", "definitions", "mapping"} and isinstance(child, dict):
                remaining = _SCHEMA_TERM_LIMIT - len(terms)
                terms.extend(str(name) for name in list(child)[:remaining])
                _append_schema_terms(child, terms, depth=depth + 1)
                continue
            if key == "discriminator" and isinstance(child, dict):
                terms.append("discriminator")
                _append_schema_terms(child, terms, depth=depth + 1)
                continue
            if key in _SCHEMA_TEXT_KEYS and isinstance(child, str | int | float | bool):
                terms.append(str(child))
                continue
            _append_schema_terms(child, terms, depth=depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _append_schema_terms(item, terms, depth=depth + 1)


def _schema_terms(tool: Tool) -> list[str]:
    terms: list[str] = []
    _append_schema_terms(tool.parameters, terms)
    return terms


def _score(tool: Tool, tokens: list[str]) -> int:
    extras = TOOL_KEYWORDS.get(tool.name, frozenset())
    # Retired names this tool replaced join the haystack so a query for the old name — or any
    # substring of it — ranks the replacement (ADR-0456 §3).
    retired = retired_names_for(tool.name)
    haystack = " ".join(
        [tool.name, tool.description or "", *extras, *retired, *_schema_terms(tool)]
    ).lower()
    return sum(1 for tok in tokens if tok in haystack)


def _rank(candidates: list[Tool], *, query: str | None, namespace: str | None) -> list[Tool]:
    """Return the ordered candidate list for the given search mode.

    - Namespace mode: filter by ``"<namespace>."`` prefix, sort lexicographically.
    - Query mode: score by substring hits in name + description + curated keywords,
      keep only tools with score > 0, sort by (score DESC, name ASC).
    - Fallback (neither): all candidates sorted by name.
    """
    if namespace is not None:
        prefix = f"{namespace}."
        return sorted(
            (t for t in candidates if t.name.startswith(prefix)),
            key=lambda t: t.name,
        )
    if query is not None:
        tokens = [tok for tok in query.lower().split() if len(tok) >= 2]
        if not tokens:
            return []
        # A query that *is* a tool name outranks every other hit, so a caller who found a
        # name in a summary result can fetch exactly that tool's schema with limit=1 rather
        # than gambling on the score/name tiebreak (ADR-0472).
        exact = query.strip().lower()
        scored = [(t, _score(t, tokens)) for t in candidates]
        hits = [(t, s) for t, s in scored if s > 0]
        hits.sort(key=lambda x: (x[0].name.lower() != exact, -x[1], x[0].name))
        return [t for t, _ in hits]
    return sorted(candidates, key=lambda t: t.name)


def _project_or_passthrough(tool: Tool, kinds: frozenset[ResourceKind] | None) -> Tool:
    if kinds is None:
        return tool
    try:
        return project_listed_tool(tool, kinds)
    except Exception:
        _log.warning(
            "provider-schema projection failed for %s in tools.search; using full schema",
            tool.name,
            exc_info=True,
        )
        return tool


def _summarize(description: str | None) -> str:
    """The first paragraph of ``description``, whitespace-collapsed onto one line.

    A KDIVE tool docstring opens with the capability sentence and then spends paragraphs on
    invocation notes, RBAC prose, and failure taxonomy. Only the first paragraph helps an
    agent *choose* a tool, so that is what a summary match carries (ADR-0472).
    """
    return " ".join((description or "").strip().split("\n\n", 1)[0].split())


def describe_tool(
    tool: Tool, kinds: frozenset[ResourceKind] | None, *, detail: SearchDetail
) -> dict[str, JsonValue]:
    """Serialise a Tool into the ``tools.search`` match shape (ADR-0472).

    Both modes carry ``name``, ``summary``, ``annotations``, and ``maturity``, so a match is
    always classifiable for safety. ``SearchDetail.FULL`` adds the complete ``description``
    and the ``input_schema``, narrowed to the composed ``kinds`` (ADR-0269).

    Args:
        tool: The registered tool to describe.
        kinds: Provider resource kinds to narrow the schema to, or None to pass it through.
        detail: Whether to include the complete description and the input schema.
    """
    annotations = {}
    raw_annotations = getattr(tool, "annotations", None)
    if raw_annotations is not None:
        annotations = raw_annotations.model_dump(mode="json", exclude_none=True)
    meta = getattr(tool, "meta", None) or {}
    described: dict[str, Any] = {
        "name": tool.name,
        "summary": _summarize(tool.description),
        "annotations": annotations,
        "maturity": meta.get("maturity"),
    }
    if detail is SearchDetail.FULL:
        described["description"] = tool.description or ""
        described["input_schema"] = _project_or_passthrough(tool, kinds).parameters
    return cast("dict[str, JsonValue]", described)


def _namespace_signal(
    all_tools: list[Tool], namespace: str, *, any_visible: bool
) -> tuple[NamespaceStatus, list[str]]:
    """Classify a ``namespace`` argument against the live registry (ADR-0472).

    Returns the status and, when the plane is live but entirely RBAC-filtered for this caller,
    the sorted any-of grants of which holding one would reveal at least one of its tools. Tool
    names are never returned: the plane's existence is already advertised to every caller in
    the server instructions' namespace table of contents, and its per-tool grants are already
    published in the generated matrix in ``docs/guide/safety-and-rbac.md``, so this discloses
    strictly less than documentation KDIVE already ships.
    """
    prefix = f"{namespace}."
    in_plane = [t.name for t in all_tools if t.name.startswith(prefix)]
    if not in_plane:
        return NamespaceStatus.UNKNOWN, []
    if any_visible:
        return NamespaceStatus.OK, []
    grants = {scope.value for name in in_plane for scope in required_scopes(name)}
    return NamespaceStatus.UNAUTHORIZED, sorted(grants)


def register(app: FastMCP, *, resolver: ProviderResolver) -> None:
    """Register the gateway tools (``tools.invoke``, ``tools.search``) on ``app``.

    Args:
        app: The FastMCP application to register tools on.
        resolver: Provider resolver used to narrow tool schemas in search results.
    """

    @app.tool(
        name="tools.invoke",
        annotations=_docmeta.destructive(),
        meta={"maturity": "implemented"},
    )
    async def tools_invoke(
        name: Annotated[
            str,
            Field(description="The registered tool to call (use tools.search to discover names)."),
        ],
        arguments: Annotated[
            dict[str, Any] | None,
            Field(description="Arguments object for that tool; omit or pass {} for no-arg tools."),
        ] = None,
    ) -> ToolResult:
        """Call any registered tool by name (gateway dispatch).

        This is the universal fallback for any capability that is advertised by the server
        but is not bound as a callable tool in your client — including lazy-loading hosts
        that materialize only a subset of the catalog. ``tools.invoke`` is always available;
        use ``tools.search`` to discover a tool name and schema, then invoke it here.

        Re-enters the server's own dispatch path with ``run_middleware=True`` so the
        inner tool runs through the full middleware stack — RBAC, telemetry, binding
        validation, and denial audit — natively, exactly as a direct call would.

        ``AuthorizationError`` from the inner call is NOT caught here; the denial-audit
        middleware handles it and converts it to an ``authorization_denied`` envelope.
        ``CategorizedError`` uses the same typed error-to-envelope conversion as direct
        tool handlers, including when FastMCP wraps it in ``ToolError``. ``NotFoundError``
        (unknown/disabled tool) and pydantic ``ValidationError`` (invalid arguments) are
        caught and converted to ``configuration_error`` envelopes.
        """
        try:
            return await app.call_tool(name, arguments or {}, run_middleware=True)
        except CategorizedError as exc:
            envelope = ToolResponse.failure_from_error("tools.invoke", exc)
            return ToolResult(structured_content=envelope.model_dump(mode="json"))
        except ToolError as exc:
            if not isinstance(exc.__cause__, CategorizedError):
                raise
            envelope = ToolResponse.failure_from_error("tools.invoke", exc.__cause__)
            return ToolResult(structured_content=envelope.model_dump(mode="json"))
        except NotFoundError:
            envelope = ToolResponse.failure(
                "tools.invoke",
                ErrorCategory.CONFIGURATION_ERROR,
                detail=(
                    f"No tool named {name!r} is registered or enabled; "
                    "discover available tools with tools.search."
                ),
            )
            return ToolResult(structured_content=envelope.model_dump(mode="json"))
        except ValidationError, FastMCPValidationError:
            # FastMCP 3.4.4 wraps a binding pydantic ValidationError in its own
            # ValidationError; both mean the caller's arguments failed schema validation.
            envelope = ToolResponse.failure(
                "tools.invoke",
                ErrorCategory.CONFIGURATION_ERROR,
                detail=f"Arguments for {name!r} failed schema validation.",
            )
            return ToolResult(structured_content=envelope.model_dump(mode="json"))

    @app.tool(
        name="tools.search",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def tools_search(
        query: Annotated[
            str | None,
            Field(description="Capability phrase to search for (e.g. 'boot a built kernel')."),
        ] = None,
        namespace: Annotated[
            str | None,
            Field(description="Browse one tool plane by prefix, e.g. 'debug' or 'runs'."),
        ] = None,
        limit: Annotated[
            int,
            Field(ge=1, le=_SEARCH_LIMIT_MAX, description="Maximum matches to return (1-50)."),
        ] = 10,
        detail: Annotated[
            SearchDetail,
            Field(
                description=(
                    "How much per-match metadata to return. 'summary' (the default) returns "
                    "name, summary, annotations, and maturity — enough to choose a tool and "
                    "judge its safety tier. 'full' additionally returns the complete "
                    "description and the input_schema you need to build arguments; it is "
                    "several times larger per match, so narrow the query or the limit first."
                )
            ),
        ] = SearchDetail.SUMMARY,
    ) -> ToolResponse:
        """Find tools by capability phrase or namespace; returns compact summaries by default.

        Two modes:
        - ``query``: lexical ranking over name, description, curated keywords, and bounded schema
          text (property names/descriptions, enum values, and discriminators); returns tools
          matching the query, highest-scoring first. A query that is exactly a tool name ranks
          that tool first.
        - ``namespace``: enumerate all tools in one plane (e.g. ``"debug"``); returns them
          sorted by name. Use this as a safety net when a query misses.

        Results are RBAC-filtered to only tools the caller could invoke. Search broadly first,
        then fetch one schema: ``tools.search(query="runs.boot", detail="full", limit=1)``
        returns exactly that tool with the ``input_schema`` ``tools.invoke`` needs. Every match
        carries ``annotations`` and ``maturity`` in both modes, so you can always classify a
        tool's safety tier before invoking it.

        ``truncated: true`` signals that more results exist beyond the returned ``limit``.

        In ``namespace`` mode the response also carries ``namespace_status``: ``"ok"`` when the
        plane has tools you can see, ``"unauthorized"`` when it is live but every tool in it is
        filtered for your grants (with ``namespace_required_grants`` naming the grants that
        would reveal one), and ``"unknown"`` when no tool carries that prefix — so an empty
        plane is never confused with a misspelled one. Query and namespace misses are logged
        for vocabulary curation.
        """
        ctx = current_context()
        all_tools = list(registered_tools(app))
        candidates = [t for t in all_tools if tool_visible(t.name, ctx)]
        ranked = _rank(candidates, query=query, namespace=namespace)
        matches = ranked[:limit]
        if not matches and query is not None:
            _log.info(
                "tool_search_miss",
                extra={"query": query, "count": 0},
            )
        kinds: frozenset[ResourceKind] | None = None
        try:
            kinds = resolver.registered_kinds()
        except Exception:
            _log.warning(
                "registered_kinds() failed in tools.search; using full schemas",
                exc_info=True,
            )
        data: dict[str, JsonValue] = {
            "matches": cast("JsonValue", [describe_tool(t, kinds, detail=detail) for t in matches]),
            "truncated": len(ranked) > limit,
        }
        if namespace is not None:
            status, grants = _namespace_signal(all_tools, namespace, any_visible=bool(ranked))
            data["namespace_status"] = status.value
            if status is NamespaceStatus.UNAUTHORIZED:
                data["namespace_required_grants"] = cast("JsonValue", grants)
            if status is not NamespaceStatus.OK:
                _log.info(
                    "tool_search_namespace_miss",
                    extra={"namespace": namespace, "namespace_status": status.value},
                )
        return ToolResponse.success("tools.search", "ok", data=data)
