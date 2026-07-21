"""``images.kernel_config`` artifact URL tool (ADR-0317)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Protocol

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

import kdive.config as config
from kdive.artifacts.storage import HeadResult
from kdive.config.core_settings import ARTIFACT_DOWNLOAD_TTL_SECONDS
from kdive.domain.errors import CategorizedError
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools.catalog.image_visibility import default_kernel_version, fetch_visible_image
from kdive.security.authz.context import RequestContext
from kdive.store.objectstore import object_store_from_env

_TOOL = "images.kernel_config"
_UNAVAILABLE_REASON = "kernel_config_unavailable"


class _ConfigStore(Protocol):
    """The narrow object-store capability the config fetch needs."""

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
    visible row with no stored config (no ``kernel_config_key``) or a missing object is a
    ``configuration_error`` with reason ``kernel_config_unavailable``.
    """
    uid = _as_uuid(image_id)
    if uid is None:
        return _invalid_uuid_error("image_id", image_id)
    entry = await fetch_visible_image(pool, ctx, uid)
    if entry is None:
        return _not_found(image_id)
    if entry.kernel_config_key is None:
        return _config_error(image_id, data={"reason": _UNAVAILABLE_REASON})
    try:
        store = store_factory()
        head = await asyncio.to_thread(store.head, entry.kernel_config_key)
        if head is None:
            return _config_error(image_id, data={"reason": _UNAVAILABLE_REASON})
        ttl = config.require(ARTIFACT_DOWNLOAD_TTL_SECONDS)
        url = await asyncio.to_thread(store.presign_get, entry.kernel_config_key, expires_in=ttl)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(image_id, exc)
    return ToolResponse.success(
        image_id,
        "available",
        suggested_next_actions=["runs.create"],
        refs={"download_uri": url},
        data={
            "default_kernel_version": default_kernel_version(entry.provenance),
            "size_bytes": head.size_bytes,
            "ttl": ttl,
        },
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the ``images.kernel_config`` read tool."""

    @app.tool(
        name=_TOOL,
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
