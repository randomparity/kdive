"""Operator/admin ``images.*`` MCP tool registration (M2.4/7, ADR-0092/0093, issue #288).

Each workflow owns its authorization and audit shape:

* ``build_publish``: platform-operator public image build/publish job admission.
* ``upload``: project-scoped private image registration from quarantine.
* ``delete``: project-scoped private image deletion with the shared reference guard.
* ``retention``: platform-admin break-glass prune/extend operations.
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.jobs.payloads import ImageBuildPayload
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.ops.images._common import (
    DELETE_TOOL,
    EXTEND_TOOL,
    PRUNE_TOOL,
    UPLOAD_TOOL,
)
from kdive.mcp.tools.ops.images.build_publish import BUILD_TOOL, PUBLISH_TOOL, build, publish
from kdive.mcp.tools.ops.images.delete import delete
from kdive.mcp.tools.ops.images.retention import extend, prune_expired
from kdive.mcp.tools.ops.images.upload import ImageUploadRequest, upload
from kdive.services.images.retention import ImageSweepStore
from kdive.services.images.upload import UploadObjectStore


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    image_store: ImageSweepStore,
    upload_store: UploadObjectStore,
) -> None:
    """Register the ``images.*`` operator/admin tools on ``app``, bound to ``pool``."""
    _register_images_build(app, pool)
    _register_images_publish(app, pool)
    _register_images_upload(app, pool, upload_store)
    _register_images_delete(app, pool)
    _register_images_prune_expired(app, pool, image_store)
    _register_images_extend(app, pool)


def _register_images_build(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(name=BUILD_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def images_build(
        provider: Annotated[
            str, Field(description="The provider whose plane builds or built the image.")
        ],
        name: Annotated[str, Field(description="The catalog image name.")],
        packages: Annotated[
            tuple[str, ...],
            Field(
                default=(),
                description="Optional package override; omitted uses the provider catalog default.",
            ),
        ] = (),
    ) -> ToolResponse:
        """Enqueue an image build job."""
        return await build(
            pool,
            current_context(),
            payload=ImageBuildPayload(provider=provider, name=name, packages=packages),
        )


def _register_images_publish(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(name=PUBLISH_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def images_publish(
        provider: Annotated[
            str, Field(description="The provider whose plane builds or built the image.")
        ],
        name: Annotated[str, Field(description="The catalog image name.")],
        packages: Annotated[
            tuple[str, ...],
            Field(
                default=(),
                description="Optional package override; omitted uses the provider catalog default.",
            ),
        ] = (),
    ) -> ToolResponse:
        """Publish a built image into the catalog."""
        return await publish(
            pool,
            current_context(),
            payload=ImageBuildPayload(provider=provider, name=name, packages=packages),
        )


def _register_images_upload(
    app: FastMCP, pool: AsyncConnectionPool, upload_store: UploadObjectStore
) -> None:
    @app.tool(name=UPLOAD_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def images_upload(
        project: Annotated[str, Field(description="The owning project for the private image.")],
        name: Annotated[str, Field(description="The catalog image name.")],
        arch: Annotated[str, Field(description="The target architecture.")],
        quarantine_key: Annotated[
            str, Field(description="The object-store key of the quarantined upload.")
        ],
        lifetime_seconds: Annotated[
            int | None,
            Field(
                default=None,
                description="TTL seconds (clamped to the ceiling); default applies.",
            ),
        ] = None,
    ) -> ToolResponse:
        """Create an image upload request."""
        return await upload(
            pool,
            current_context(),
            upload_store,
            ImageUploadRequest(
                project=project,
                name=name,
                arch=arch,
                quarantine_key=quarantine_key,
                lifetime_seconds=lifetime_seconds,
            ),
        )


def _register_images_delete(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name=DELETE_TOOL, annotations=_docmeta.destructive(), meta={"maturity": "implemented"}
    )
    async def images_delete(
        image_id: Annotated[str, Field(description="The private catalog image to delete.")],
    ) -> ToolResponse:
        """Delete a private image catalog entry (project-scoped). Irreversible.

        Removes the catalog entry and its backing object permanently; there is no undo.
        A shared reference guard rejects deletion while the image is still referenced.
        """
        return await delete(pool, current_context(), image_id=image_id)


def _register_images_prune_expired(
    app: FastMCP, pool: AsyncConnectionPool, image_store: ImageSweepStore
) -> None:
    @app.tool(name=PRUNE_TOOL, annotations=_docmeta.destructive(), meta={"maturity": "implemented"})
    async def images_prune_expired(
        reason: Annotated[
            str, Field(description="Mandatory non-blank break-glass justification (audited).")
        ],
    ) -> ToolResponse:
        """Permanently prune every expired image entry (platform-admin break-glass). Irreversible.

        A break-glass sweep gated on ``platform_admin``: it deletes all past-lifetime image
        entries and their backing objects in one pass, with no per-image confirmation and no
        undo. ``reason`` is audited. Use ``images.extend`` to save an entry before it expires.
        """
        return await prune_expired(pool, current_context(), reason=reason, image_store=image_store)


def _register_images_extend(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name=EXTEND_TOOL, annotations=_docmeta.destructive(), meta={"maturity": "implemented"}
    )
    async def images_extend(
        image_id: Annotated[str, Field(description="The private image whose lifetime to extend.")],
        seconds: Annotated[int, Field(description="Seconds from now (clamped to the ceiling).")],
        reason: Annotated[
            str, Field(description="Mandatory non-blank break-glass justification (audited).")
        ],
    ) -> ToolResponse:
        """Extend an image catalog entry lease."""
        return await extend(
            pool, current_context(), image_id=image_id, seconds=seconds, reason=reason
        )


__all__ = [
    "register",
]
