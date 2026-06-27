"""``runs.validate_profile`` — no-insert build-profile validation (#839, ADR-0259).

A read-only, auth-only tool that runs the same checks ``runs.create`` runs — ``BuildProfile``
structural parse plus build-host/source-kind compatibility — over the **raw** profile document,
and returns the project's typed :class:`~kdive.mcp.responses.ToolResponse` envelope **without**
inserting a Run, consuming capacity, or requiring an Investigation/System/Allocation.

The parameter is the raw document (not the parsed ``ExternalBuildProfile | ServerBuildProfile``
union ``runs.create`` types its ``build_profile`` as), so the handler — not the FastMCP boundary
— produces the verdict via :meth:`BuildProfile.parse`; that is the whole point, since a union at
the boundary would re-create the merged, source-ambiguous Pydantic error this tool exists to
replace.

The compatibility step mirrors ``runs.create``'s create-time twin
(:func:`~kdive.services.runs.admission._compat_block_response`): it defaults an omitted
``build_host`` to ``"worker-local"`` and **allows** an unregistered named host (the host may be
registered before build), so the verdict matches the create-time verdict exactly — pinned by a
parity test. Host availability (enabled/reachable/at-capacity) is not checked here; like
``runs.create``, that is deferred to ``runs.build``.
"""

from __future__ import annotations

from typing import cast

from psycopg_pool import AsyncConnectionPool

from kdive.db.build_hosts import get_by_name
from kdive.domain.errors import CategorizedError
from kdive.mcp.responses import ToolResponse
from kdive.profiles.build import (
    BuildProfile,
    ExternalBuildProfile,
    ServerBuildProfile,
    dump_build_profile,
    is_git_source,
)
from kdive.profiles.types import BuildProfileInput
from kdive.serialization import JsonValue
from kdive.services.runs.build_host_selection import check_source_kind_compatibility

_OBJECT_ID = "profile-validation"
_FIX_NEXT = ["runs.profile_examples"]
_OK_NEXT = ["runs.create"]
_DEFAULT_BUILD_HOST = "worker-local"


async def validate_build_profile(
    pool: AsyncConnectionPool, build_profile: BuildProfileInput
) -> ToolResponse:
    """Validate a raw build-profile document, returning the typed envelope without a Run.

    Args:
        pool: The connection pool, used only on the server lane for the build-host lookup; the
            external lane and a parse failure never open a connection.
        build_profile: The raw, unparsed profile document.

    Returns:
        A ``valid`` envelope (with the normalized profile and, for the server lane, the resolved
        build-host facts), or a ``configuration_error`` envelope for a parse failure or a
        build-host/source-kind incompatibility.
    """
    try:
        parsed = BuildProfile.parse(build_profile)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(_OBJECT_ID, exc, suggested_next_actions=_FIX_NEXT)
    if isinstance(parsed, ExternalBuildProfile):
        return _valid({"source": "external"}, parsed)
    return await _validate_server(pool, parsed)


async def _validate_server(pool: AsyncConnectionPool, parsed: ServerBuildProfile) -> ToolResponse:
    """Resolve the named (or default) build host and check source-kind compatibility."""
    name = parsed.build_host or _DEFAULT_BUILD_HOST
    async with pool.connection() as conn:
        host = await get_by_name(conn, name)
    source_kind = "git" if is_git_source(parsed) else "warm-tree"
    data: dict[str, JsonValue] = {
        "source": "server",
        "build_host": name,
        "build_host_registered": host is not None,
        "host_kind": host.kind.value if host is not None else None,
        "source_kind": source_kind,
    }
    if host is None:
        return _valid(data, parsed)
    try:
        check_source_kind_compatibility(
            host_kind=host.kind, is_git=is_git_source(parsed), build_host=name
        )
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(_OBJECT_ID, exc, suggested_next_actions=_FIX_NEXT)
    return _valid(data, parsed)


def _valid(
    data: dict[str, JsonValue], parsed: ExternalBuildProfile | ServerBuildProfile
) -> ToolResponse:
    """Build the ``valid`` envelope, attaching the normalized, paste-ready profile echo."""
    payload = dict(data)
    # dump_build_profile emits JSON by construction; SerializedBuildProfile is typed as the
    # looser Mapping[str, object], so narrow it to the response's JsonValue.
    payload["profile"] = cast(JsonValue, dump_build_profile(parsed))
    return ToolResponse.success(
        _OBJECT_ID, "valid", data=payload, suggested_next_actions=list(_OK_NEXT)
    )
