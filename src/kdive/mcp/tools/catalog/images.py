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

import asyncio
from collections.abc import Callable
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

import kdive.config as config
from kdive.artifacts.storage import HeadResult
from kdive.config.core_settings import ARTIFACT_DOWNLOAD_TTL_SECONDS
from kdive.domain.catalog.images import ImageCatalogEntry, ImageVisibility
from kdive.domain.errors import CategorizedError
from kdive.images.capability_signals import REGISTERED_SIGNALS
from kdive.images.kdump_support import (
    DEFAULT_KERNEL_BASIS,
    KernelVersion,
)
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, InvalidCursor, _short_id
from kdive.mcp.tools._common import ConfigErrorReason as _ConfigErrorReason
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import config_error as _config_error
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
from kdive.store.objectstore import object_store_from_env

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


def _compact_os(provenance: dict[str, Any]) -> dict[str, JsonValue]:
    """Project ``provenance["os_release"]`` into a compact ``{id[, version_id]}`` identity.

    ADR-0311. Empty when there is no ``os_release`` record, it is not a dict, or it carries no
    ``id`` — a record without a distro id is not a usable identity, so a bare version is never
    surfaced. ``version_id`` is included only when present (a rolling distro may omit it).
    """
    record = provenance.get("os_release")
    if not isinstance(record, dict) or not record.get("id"):
        return {}
    compact: dict[str, JsonValue] = {"id": str(record["id"])}
    version_id = record.get("version_id")
    if version_id:
        compact["version_id"] = str(version_id)
    return compact


def _default_kernel_version(provenance: dict[str, Any]) -> str:
    """The build-recorded default kernel version, or ``""`` when absent (ADR-0317).

    The image's default kernel for informed agent selection: the version the image ships and
    boots by default, captured at build time. ``""`` when the build could not name a single
    baseline kernel (zero/many) or the row predates the feature.
    """
    value = provenance.get("default_kernel_version")
    return str(value) if value else ""


def _row_envelope(entry: ImageCatalogEntry) -> ToolResponse:
    """One image row as a sub-envelope: identity, scope, publish state, and merit signals.

    Carries the build-fact ``capabilities`` tags, a compact verified ``os`` identity, and the
    operator-attested ``description`` so an agent can compare images on merit in one call rather
    than an N+1 ``images.describe`` fan-out (ADR-0311).
    """
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
            "capabilities": [cap.value for cap in entry.capabilities],
            "os": _compact_os(entry.provenance),
            "default_kernel_version": _default_kernel_version(entry.provenance),
            "description": entry.description or "",
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


async def _fetch_visible_image(
    pool: AsyncConnectionPool, ctx: RequestContext, uid: UUID
) -> ImageCatalogEntry | None:
    """The catalog row ``uid`` visible to ``ctx`` (public, or owned-private viewer), else ``None``.

    Filters in SQL on the caller's viewer-authorized project set, so an unauthorized private row
    never leaves the database; the ``None`` case is byte-identical for an absent or an invisible id
    (no existence/membership leak). Shared by ``images.describe`` and ``images.kernel_config``.
    """
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
    return ImageCatalogEntry.model_validate(row) if row is not None else None


def _capability_signals(entry: ImageCatalogEntry, basis: KernelVersion) -> dict[str, JsonValue]:
    """The computed capability signals for ``entry`` against ``basis`` (ADR-0286/0295).

    Iterates the registered signals, keying each rendered block by signal name: ``kdump`` (the
    makedumpfile-vs-target-kernel capability) and ``direct_kernel`` (whether the image's ``/boot``
    holds exactly one non-rescue kernel, so a direct-kernel provision can select a baseline
    unambiguously). Each signal reads a build-recorded provenance operand and degrades to a
    non-confident status when the operand is absent, so a reader never raises on image data.
    """
    return {sig.name: sig.render(entry, basis) for sig in REGISTERED_SIGNALS}


def _describe_envelope(entry: ImageCatalogEntry, basis: KernelVersion) -> ToolResponse:
    """Full per-image detail; withholds the staged ``path`` and the S3 ``object_key``.

    Surfaces ``provenance`` verbatim (build metadata, no secret values), the boot layout, digest,
    capabilities, scope, publish state, and the computed ``capability_signals`` block (each signal
    keyed by name, computed for ``basis``; the signals are ``kdump`` and ``direct_kernel``).
    ``provenance_attested`` is true when the provenance is an operator declaration (an ``s3``
    image's ``[image.attested]`` operands, ADR-0323) rather than a KDIVE-verified fact; the same
    distinction appears per-signal as ``capability_signals[*].basis`` (``operator_attested`` vs
    ``build_verified``) on any signal whose operand is present. ``expires_at`` is an ISO-8601 string
    when set (a ``datetime`` is not a ``JsonValue``), ``""`` otherwise.
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
            "capabilities": [cap.value for cap in entry.capabilities],
            "os": _compact_os(entry.provenance),
            "default_kernel_version": _default_kernel_version(entry.provenance),
            "description": entry.description or "",
            "provenance": entry.provenance,
            "provenance_attested": entry.provenance_attested,
            "capability_signals": _capability_signals(entry, basis),
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
    selects the kernel the ``data.capability_signals`` kdump capability is computed against,
    defaulting to the characterized basis; a malformed value is a ``configuration_error``
    (``invalid_version``).
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
    entry = await _fetch_visible_image(pool, ctx, uid)
    if entry is None:
        return _not_found(image_id)
    return _describe_envelope(entry, basis)


_KERNEL_CONFIG_TOOL = "images.kernel_config"
_KERNEL_CONFIG_UNAVAILABLE = "kernel_config_unavailable"


class _ConfigStore(Protocol):
    """The narrow object-store capability the config fetch needs (an ObjectStore satisfies it)."""

    def head(self, key: str) -> HeadResult | None: ...
    def presign_get(self, key: str, *, expires_in: int) -> str: ...


async def kernel_config(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    image_id: str,
    *,
    store_factory: Callable[[], _ConfigStore] = object_store_from_env,
) -> ToolResponse:
    """Mint a presigned download URL for a catalog image's kernel ``.config`` (ADR-0317).

    Resolves the row under the ``images.describe`` visibility predicate (public, or owned-private
    with ``viewer``), HEADs the stored ``/boot/config-<ver>`` object, and presigns a short-lived
    GET (``KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS``). A malformed id is a ``configuration_error``; a
    valid id with no visible row is ``not_found`` (byte-identical whether absent or invisible). A
    visible row with no stored config (no ``kernel_config_key`` — a staged/pre-feature image or a
    best-effort config-write failure) or a missing object is a ``configuration_error`` with reason
    ``kernel_config_unavailable``. The config is never inspected or validated; the egress is not
    audited (REDACTED-class, visibility-gated like ``images.describe``).
    """
    uid = _as_uuid(image_id)
    if uid is None:
        return _invalid_uuid_error("image_id", image_id)
    entry = await _fetch_visible_image(pool, ctx, uid)
    if entry is None:
        return _not_found(image_id)
    if entry.kernel_config_key is None:
        return _config_error(image_id, data={"reason": _KERNEL_CONFIG_UNAVAILABLE})
    try:
        store = store_factory()
        head = await asyncio.to_thread(store.head, entry.kernel_config_key)
        if head is None:
            return _config_error(image_id, data={"reason": _KERNEL_CONFIG_UNAVAILABLE})
        ttl = config.require(ARTIFACT_DOWNLOAD_TTL_SECONDS)
        url = await asyncio.to_thread(store.presign_get, entry.kernel_config_key, expires_in=ttl)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(image_id, exc)
    return ToolResponse.success(
        image_id,
        "available",
        suggested_next_actions=[_KERNEL_CONFIG_TOOL],
        refs={"download_uri": url},
        data={
            "default_kernel_version": _default_kernel_version(entry.provenance),
            "size_bytes": head.size_bytes,
            "ttl": ttl,
        },
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the ``images.list``/``images.describe`` read tools on ``app``, bound to ``pool``."""

    @app.tool(
        name=_LIST_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def images_list(
        limit: Annotated[
            int, Field(description=f"Maximum rows returned (capped at {MAX_LIST_LIMIT}).")
        ] = DEFAULT_LIST_LIMIT,
        cursor: Annotated[
            str | None,
            Field(description="Opaque continuation cursor from a prior page's next_cursor."),
        ] = None,
    ) -> ToolResponse:
        """List visible image catalog entries across publish states.

        Each row carries the build-fact ``data.capabilities``, a compact verified ``data.os``
        identity, and ``data.default_kernel_version`` (the kernel the image ships and boots by
        default, ``""`` when unknown) so an agent can compare images on merit — distro, version,
        default kernel — in one call. The publish state appears as the item envelope ``status`` and
        as ``data.state``. Keyset-paginated: when ``data.truncated`` is true, pass
        ``data.next_cursor`` back as ``cursor`` for the next page.
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
                    "Target kernel version (e.g. 7.1) to compute the data.capability_signals "
                    "kdump capability against; defaults to the characterized basis when omitted."
                )
            ),
        ] = None,
    ) -> ToolResponse:
        """Return full detail for one catalog image visible to the caller.

        Includes boot layout, digest, capabilities, scope, publish state,
        ``data.default_kernel_version`` (the image's default kernel, ``""`` when unknown), build
        ``provenance`` (with captured
        ``package_versions``/``makedumpfile_version``/``boot_kernel_count`` when present), and
        computed ``data.capability_signals`` (each signal keyed by name): ``kdump``
        (the capability for ``target_kernel``, kernel basis disclosed) and ``direct_kernel``
        (``status`` ``provisionable`` when ``/boot`` holds exactly one non-rescue kernel, else
        ``not_provisionable``/``unverified`` — read it before a direct-kernel provision so a
        multi-kernel image does not burn an allocation on a fail-closed selection). A signal reads
        ``unverified`` whenever its operand was never recorded — the normal, honest state for an
        externally-baked image the operator has not attested and KDIVE has not built. When the
        operand *is* present, ``basis`` discloses its evidence: ``build_verified`` (recorded by a
        KDIVE build/publish) or ``operator_attested`` (declared by the operator, also flagged by
        ``data.provenance_attested``); an ``operator_attested`` signal is a claim kdive did not
        verify.
        """
        return await describe_image(pool, current_context(), image_id, target_kernel)

    @app.tool(
        name=_KERNEL_CONFIG_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def images_kernel_config(
        image_id: Annotated[str, Field(description="The catalog image row id (UUID).")],
    ) -> ToolResponse:
        """Return a short-lived download URL for the image's kernel ``.config`` starting point.

        The URL under ``refs.download_uri`` fetches the image's ``/boot/config-<ver>`` — a
        known-good config to build a kernel from, never validated by kdive.
        ``data.default_kernel_version`` names the version, ``data.size_bytes`` the config size, and
        ``data.ttl`` the URL lifetime. An image with no offered config (a staged or pre-feature
        image, or one whose ``/boot`` lacked a single kernel/config) returns a
        ``configuration_error`` with ``data.reason`` = ``kernel_config_unavailable``.
        """
        return await kernel_config(pool, current_context(), image_id)
