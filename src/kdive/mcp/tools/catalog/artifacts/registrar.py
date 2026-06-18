"""The public `artifacts.*` MCP tool registrar."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.catalog.artifacts import reads as artifact_reads
from kdive.mcp.tools.catalog.artifacts import uploads as artifact_uploads
from kdive.mcp.tools.catalog.artifacts.expected_uploads import (
    expected_uploads as _expected_uploads,
)
from kdive.providers.core.resolver import ProviderResolver


def register(app: FastMCP, pool: AsyncConnectionPool, *, resolver: ProviderResolver) -> None:
    """Register the `artifacts.*` tools on ``app``, bound to ``pool``."""
    _register_artifacts_list(app, pool)
    _register_artifacts_get(app, pool)
    _register_artifacts_search_text(app, pool)
    _register_artifacts_create_run_upload(app, pool, resolver)
    _register_artifacts_create_system_upload(app, pool, resolver)
    _register_artifacts_expected_uploads(app)


def _register_artifacts_list(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="artifacts.list",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta(
            "partial",
            reason=_docmeta.MaturityReason.LIVE_DEPENDENCY,
            detail=(
                "Lists redacted artifacts a System produced; those rows only exist after "
                "a live build/boot/capture path runs, exercised under the gated live "
                "markers."
            ),
            promotion=(
                "A non-gated test asserts the listing against artifacts a real run produced, "
                "or a recorded live_stack run does."
            ),
        ),
    )
    async def artifacts_list(
        system_id: Annotated[
            str, Field(description="The System whose redacted artifacts to list.")
        ],
    ) -> ToolResponse:
        """List the redacted artifacts for a System. Requires viewer."""
        return await artifact_reads.artifacts_list(pool, current_context(), system_id=system_id)


def _register_artifacts_get(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="artifacts.get",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta(
            "partial",
            reason=_docmeta.MaturityReason.LIVE_DEPENDENCY,
            detail=(
                "Fetches a redacted artifact's bytes; the artifact only exists after a live "
                "build/boot/capture path runs, exercised under the gated live markers."
            ),
            promotion=(
                "A non-gated test fetches inline + presigned content for an artifact a real "
                "run produced, or a recorded live_stack run does."
            ),
        ),
    )
    async def artifacts_get(
        artifact_id: Annotated[
            str,
            Field(description="The redacted artifact to fetch (sensitive ids are not-found)."),
        ],
    ) -> ToolResponse:
        """Fetch one redacted artifact's content by id.

        Returns the object ref plus, best-effort, the redacted bytes inline in
        `data.content` (capped at KDIVE_ARTIFACT_INLINE_MAX_BYTES; larger artifacts
        set `content_omitted` and are retrieved via `refs.download_uri`) and a
        presigned `refs.download_uri`. Requires viewer; sensitive ids are not-found.
        """
        return await artifact_reads.artifacts_get(pool, current_context(), artifact_id=artifact_id)


def _register_artifacts_search_text(app: FastMCP, pool: AsyncConnectionPool) -> None:
    read_handlers = artifact_reads.ArtifactReadHandlers()

    @app.tool(
        name="artifacts.search_text",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta(
            "partial",
            reason=_docmeta.MaturityReason.LIVE_DEPENDENCY,
            detail=(
                "Searches a redacted artifact's text; the artifact only exists after a live "
                "build/boot/capture path runs, exercised under the gated live markers."
            ),
            promotion=(
                "A non-gated test searches an artifact a real run produced, or a recorded "
                "live_stack run does."
            ),
        ),
    )
    async def artifacts_search_text(
        artifact_id: Annotated[str, Field(description="The redacted System artifact id.")],
        pattern: Annotated[
            str,
            Field(description="Literal OR search pattern, e.g. '__d_lookup' or 'panic'."),
        ],
        before_lines: Annotated[int, Field(description="Context lines before each match.")] = 2,
        after_lines: Annotated[int, Field(description="Context lines after each match.")] = 4,
        max_matches: Annotated[int, Field(description="Maximum match windows to return.")] = 20,
    ) -> ToolResponse:
        """Search a redacted System artifact with bounded literal line context."""
        return await read_handlers.artifacts_search_text(
            pool,
            current_context(),
            request=artifact_reads.ArtifactSearchRequest(
                artifact_id=artifact_id,
                pattern=pattern,
                before_lines=before_lines,
                after_lines=after_lines,
                max_matches=max_matches,
            ),
        )


def _register_artifacts_create_run_upload(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="artifacts.create_run_upload",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def artifacts_create_run_upload(
        run_id: Annotated[str, Field(description="The external-build Run id.")],
        artifacts: Annotated[
            list[artifact_uploads.ArtifactDeclaration],
            Field(description="Declared build artifacts: [{name, sha256 (base64), size_bytes}]."),
        ],
    ) -> ToolResponse:
        """Mint presigned PUTs for an external Run's build artifacts. Requires operator."""
        return await artifact_uploads.create_run_upload(
            pool,
            current_context(),
            run_id=run_id,
            artifacts=artifacts,
            resolver=resolver,
        )


def _register_artifacts_create_system_upload(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="artifacts.create_system_upload",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def artifacts_create_system_upload(
        system_id: Annotated[str, Field(description="The DEFINED System id.")],
        artifacts: Annotated[
            list[artifact_uploads.ArtifactDeclaration],
            Field(description="Declared rootfs artifact: [{name, sha256 (base64), size_bytes}]."),
        ],
    ) -> ToolResponse:
        """Mint a presigned PUT for a DEFINED System's rootfs. Requires operator."""
        return await artifact_uploads.create_system_upload(
            pool,
            current_context(),
            system_id=system_id,
            artifacts=artifacts,
            resolver=resolver,
        )


def _register_artifacts_expected_uploads(app: FastMCP) -> None:
    @app.tool(
        name="artifacts.expected_uploads",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def artifacts_expected_uploads() -> ToolResponse:
        """Return the accepted upload-artifact names per owner-kind. Requires a token."""
        # Auth-only (ADR-0117): the verifier already gated the transport; enforce token
        # presence as defence-in-depth. No platform/project gate, no audit — the
        # projection is the public upload-name vocabulary only (ADR-0166).
        current_context()
        return _expected_uploads()
