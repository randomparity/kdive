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
from pydantic import BaseModel, ConfigDict, Field

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


class ImageBuildRequest(BaseModel):
    """MCP-facing public image build/publish request shared by both tools."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(description="The provider whose plane builds or built the image.")
    name: str = Field(description="The catalog image name.")
    packages: tuple[str, ...] = Field(
        default=(),
        description="Optional package override; omitted uses the provider catalog default.",
    )

    def to_payload(self) -> ImageBuildPayload:
        """Convert the MCP request into the durable IMAGE_BUILD job payload."""
        return ImageBuildPayload(
            provider=self.provider,
            name=self.name,
            packages=self.packages,
        )


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
        request: Annotated[
            ImageBuildRequest,
            Field(description="Public image build request."),
        ],
    ) -> ToolResponse:
        """Enqueue an image build job."""
        return await build(pool, current_context(), payload=request.to_payload())


def _register_images_publish(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(name=PUBLISH_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def images_publish(
        request: Annotated[
            ImageBuildRequest,
            Field(description="Public image publish request."),
        ],
    ) -> ToolResponse:
        """Publish a built image into the catalog."""
        return await publish(pool, current_context(), payload=request.to_payload())


def _register_images_upload(
    app: FastMCP, pool: AsyncConnectionPool, upload_store: UploadObjectStore
) -> None:
    @app.tool(name=UPLOAD_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def images_upload(
        request: Annotated[
            ImageUploadRequest,
            Field(description="Private image upload registration request."),
        ],
    ) -> ToolResponse:
        """Create an image upload request."""
        return await upload(pool, current_context(), upload_store, request)


def _register_images_delete(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name=DELETE_TOOL, annotations=_docmeta.destructive(), meta={"maturity": "implemented"}
    )
    async def images_delete(
        image_id: Annotated[str, Field(description="The private catalog image to delete.")],
    ) -> ToolResponse:
        """Delete an image catalog entry."""
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
        """Prune expired image catalog entries."""
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
    "ImageBuildRequest",
    "register",
]
