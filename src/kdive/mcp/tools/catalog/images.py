"""``images.list`` read tool: the RBAC-filtered catalog view (M2.4/7, ADR-0092/0093).

The ``kdivectl images list`` server seam. A caller sees every ``public`` catalog row plus the
``private`` rows owned by projects where their token satisfies ``viewer``, and never another
project's private image. The filter is applied **in SQL** (a parameterized ``owner = ANY`` over
the viewer-authorized set) so an unauthorized private row never leaves the database. Unlike
:func:`kdive.images.catalog.resolve_rootfs` (which returns only the one bootable ``registered``
row), the operator list surfaces every state — a ``defined`` baseline and a ``pending`` publish
included — so the operator can see in-flight and seeded images.
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.catalog.images import ImageCatalogEntry, ImageVisibility
from kdive.images.kdump_support import (
    DEFAULT_KERNEL_BASIS,
    KernelVersion,
    kdump_capability,
)
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, InvalidCursor, _short_id
from kdive.mcp.tools._common import ConfigErrorReason as _ConfigErrorReason
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import config_error_reason as _config_error_reason
from kdive.mcp.tools._common import decode_cursor as _decode_cursor
from kdive.mcp.tools._common import encode_cursor as _encode_cursor
from kdive.mcp.tools._common import invalid_cursor_error as _invalid_cursor_error
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools._common import paginate as _paginate
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, projects_with_role
from kdive.serialization import JsonValue

_LIST_TOOL = "images.list"
_LIST_TAG = "images.list"
_DESCRIBE_TOOL = "images.describe"

_LIST_SQL = """
    SELECT *
    FROM image_catalog
    WHERE (visibility = %(public)s
           OR (visibility = %(private)s AND owner = ANY(%(projects)s)))
      AND (%(after)s::boolean IS FALSE
           OR (provider, name, arch) > (%(p)s, %(n)s, %(a)s))
    ORDER BY provider, name, arch
    LIMIT %(limit)s
"""


def _row_envelope(entry: ImageCatalogEntry) -> ToolResponse:
    """One image row as a sub-envelope: identity, scope, and publish state in ``data``."""
    return ToolResponse.success(
        str(entry.id),
        entry.state.value,
        data={
            "provider": entry.provider,
            "name": entry.name,
            "arch": entry.arch,
            "visibility": entry.visibility.value,
            "owner": entry.owner or "",
            "state": entry.state.value,
            "volume": entry.volume or "",
        },
    )


async def list_images(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    limit: int = DEFAULT_LIST_LIMIT,
    cursor: str | None = None,
) -> ToolResponse:
    """List the public catalog images plus the caller's projects' private images.

    The private filter is parameterized on the caller's viewer-authorized project set, so a
    private row owned by an unauthorized project is never selected. Keyset-paginated over the
    ``(provider, name, arch)`` natural key (ADR-0192): fetches one row past ``limit`` to set
    ``data.truncated`` / ``data.next_cursor`` from the last kept row's key.
    """
    capped = _clamp_list_limit(limit)
    after_parts: list[str] | None = None
    if cursor:
        try:
            after_parts = _decode_cursor(_LIST_TAG, cursor, arity=3)
        except InvalidCursor:
            return _invalid_cursor_error("images")
    with bind_context(principal=ctx.principal):
        params = {
            "public": ImageVisibility.PUBLIC.value,
            "private": ImageVisibility.PRIVATE.value,
            "projects": projects_with_role(ctx, Role.VIEWER),
            "after": after_parts is not None,
            "p": after_parts[0] if after_parts else "",
            "n": after_parts[1] if after_parts else "",
            "a": after_parts[2] if after_parts else "",
            "limit": capped + 1,
        }
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_LIST_SQL, params)
            rows = await cur.fetchall()
    kept, truncated = _paginate(rows, capped)
    items = [_row_envelope(ImageCatalogEntry.model_validate(row)) for row in kept]
    next_cursor = (
        _encode_cursor(_LIST_TAG, (kept[-1]["provider"], kept[-1]["name"], kept[-1]["arch"]))
        if truncated and kept
        else None
    )
    return ToolResponse.collection(
        "images",
        "ok",
        items,
        suggested_next_actions=[_LIST_TOOL],
        data={"truncated": truncated, "next_cursor": next_cursor},
    )


_DESCRIBE_SQL = """
    SELECT *
    FROM image_catalog
    WHERE id = %(id)s
      AND (visibility = %(public)s
           OR (visibility = %(private)s AND owner = ANY(%(projects)s)))
"""


def _kdump_block(entry: ImageCatalogEntry, basis: KernelVersion) -> dict[str, JsonValue]:
    """The computed kdump capability for ``entry`` against ``basis`` (ADR-0253).

    Reads the build-recorded ``provenance["makedumpfile_version"]`` (``None`` when absent or not a
    string) and whether the image carries the ``"kdump"`` tooling tag, then computes the capability,
    echoing the kernel basis it was computed against. A reader never raises on image data — an
    unparseable stored version degrades to ``unverified`` inside :func:`kdump_capability`.
    """
    raw = entry.provenance.get("makedumpfile_version")
    cap = kdump_capability(
        makedumpfile_version=raw if isinstance(raw, str) and raw else None,
        target_kernel=basis,
        kdump_tooling="kdump" in entry.capabilities,
    )
    return {
        "makedumpfile_version": raw if isinstance(raw, str) else "",
        "target_kernel": cap.target_kernel,
        "capability": cap.status,
        "min_makedumpfile_required": cap.min_makedumpfile_required,
        "note": cap.note,
    }


def _describe_envelope(entry: ImageCatalogEntry, basis: KernelVersion) -> ToolResponse:
    """Full per-image detail; withholds the staged ``path`` and the S3 ``object_key``.

    Surfaces ``provenance`` verbatim (build metadata, no secret values), the boot layout, digest,
    capabilities, scope, publish state, and a computed ``kdump`` block (capability for ``basis``).
    ``expires_at`` is an ISO-8601 string when set (a ``datetime`` is not a ``JsonValue``), ``""``
    otherwise.
    """
    return ToolResponse.success(
        str(entry.id),
        entry.state.value,
        data={
            "provider": entry.provider,
            "name": entry.name,
            "arch": entry.arch,
            "format": entry.format,
            "root_device": entry.root_device,
            "visibility": entry.visibility.value,
            "owner": entry.owner or "",
            "state": entry.state.value,
            "digest": entry.digest or "",
            "capabilities": list(entry.capabilities),
            "provenance": entry.provenance,
            "kdump": _kdump_block(entry, basis),
            "volume": entry.volume or "",
            "expires_at": entry.expires_at.isoformat() if entry.expires_at else "",
            "managed_by": entry.managed_by.value,
        },
        suggested_next_actions=[_LIST_TOOL],
    )


async def describe_image(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    image_id: str,
    target_kernel: str | None = None,
) -> ToolResponse:
    """Return one catalog image visible to the caller, addressed by row id (ADR-0252/0253).

    Visibility reuses the ``images.list`` predicate (public, or owned-private with viewer),
    filtered in SQL so an unauthorized private row never leaves the database. A malformed id is a
    ``configuration_error``; a valid id with no visible row is ``not_found`` (byte-identical
    whether absent or invisible — no existence/membership leak). ``target_kernel`` (optional)
    selects the kernel the ``data.kdump`` capability is computed against, defaulting to the
    characterized basis; a malformed value is a ``configuration_error`` (``invalid_version``).
    """
    uid = _as_uuid(image_id)
    if uid is None:
        return _invalid_uuid_error("image_id", image_id)
    basis = DEFAULT_KERNEL_BASIS
    if target_kernel is not None:
        try:
            basis = KernelVersion.parse(target_kernel)
        except ValueError:
            return _config_error_reason(
                target_kernel,
                _ConfigErrorReason.INVALID_VERSION,
                detail=f"target_kernel {_short_id(target_kernel)!r} is not a recognized "
                "kernel version",
            )
    with bind_context(principal=ctx.principal):
        params = {
            "id": str(uid),
            "public": ImageVisibility.PUBLIC.value,
            "private": ImageVisibility.PRIVATE.value,
            "projects": projects_with_role(ctx, Role.VIEWER),
        }
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_DESCRIBE_SQL, params)
            row = await cur.fetchone()
    if row is None:
        return _not_found(image_id)
    return _describe_envelope(ImageCatalogEntry.model_validate(row), basis)


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the ``images.list``/``images.describe`` read tools on ``app``, bound to ``pool``."""

    @app.tool(
        name=_LIST_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def images_list(
        limit: Annotated[
            int, Field(description="Maximum rows returned (capped at 200).")
        ] = DEFAULT_LIST_LIMIT,
        cursor: Annotated[
            str | None,
            Field(description="Opaque continuation cursor from a prior page's next_cursor."),
        ] = None,
    ) -> ToolResponse:
        """List published image catalog entries.

        Keyset-paginated: when ``data.truncated`` is true, pass ``data.next_cursor`` back as
        ``cursor`` for the next page.
        """
        return await list_images(pool, current_context(), limit=limit, cursor=cursor)

    @app.tool(
        name=_DESCRIBE_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def images_describe(
        image_id: Annotated[str, Field(description="The catalog image row id (UUID) to describe.")],
        target_kernel: Annotated[
            str | None,
            Field(
                description=(
                    "Target kernel version (e.g. 7.1) to compute the data.kdump capability "
                    "against; defaults to the characterized basis when omitted."
                )
            ),
        ] = None,
    ) -> ToolResponse:
        """Return full detail for one catalog image visible to the caller.

        Includes boot layout, digest, capabilities, scope, publish state, build ``provenance``
        (with captured ``package_versions``/``makedumpfile_version`` when present), and a computed
        ``data.kdump`` block (capability for ``target_kernel``, with the kernel basis disclosed).
        """
        return await describe_image(pool, current_context(), image_id, target_kernel)
