"""The public `artifacts.*` MCP tool registrar."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.catalog.artifacts import raw_fetch as artifact_raw_fetch
from kdive.mcp.tools.catalog.artifacts import reads as artifact_reads
from kdive.mcp.tools.catalog.artifacts import uploads as artifact_uploads
from kdive.mcp.tools.catalog.artifacts.expected_uploads import (
    expected_uploads as _expected_uploads,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.artifacts.artifact_search import (
    AFTER_LINES_RANGE,
    BEFORE_LINES_RANGE,
    MAX_MATCHES_RANGE,
)
from kdive.serialization import JsonValue


def _declaration_schema_extra(examples: Sequence[JsonValue]) -> dict[str, object]:
    """Advertise the upload-declaration item schema + ``examples`` (ADR-0173).

    Merged into the ``artifacts`` array parameter's advertised JSON Schema so a black-box
    client can discover the declaration shape. Returns a fresh dict so pydantic/FastMCP
    never mutates the shared module constants. Advertisement only: the runtime parameter
    stays a permissive ``Mapping`` (``ArtifactDeclaration``), so a malformed declaration
    still reaches the ADR-0166 self-correcting validators rather than a boundary error. The
    item *shape* is shared across both upload tools; ``examples`` carry each tool's
    accepted artifact names.
    """
    return {
        "items": artifact_uploads.UPLOAD_DECLARATION_ITEM_SCHEMA,
        "examples": examples,
    }


def register(app: FastMCP, pool: AsyncConnectionPool, *, resolver: ProviderResolver) -> None:
    """Register the `artifacts.*` tools on ``app``, bound to ``pool``."""
    _register_artifacts_list(app, pool)
    _register_artifacts_get(app, pool)
    _register_artifacts_fetch_raw(app, pool)
    _register_artifacts_search_text(app, pool)
    _register_artifacts_create_run_upload(app, pool, resolver)
    _register_artifacts_create_system_upload(app, pool, resolver)
    _register_artifacts_expected_uploads(app)


def _register_artifacts_list(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="artifacts.list",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
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
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def artifacts_get(
        artifact_id: Annotated[
            str,
            Field(description="The redacted artifact to fetch (sensitive ids are not-found)."),
        ],
        byte_offset: Annotated[
            int,
            Field(
                description=(
                    "Start byte of the inline window (0-based; a negative value reads from "
                    "the start). Page through a large artifact with the returned "
                    'data.next_offset until data.content_truncated is "false".'
                )
            ),
        ] = 0,
        max_bytes: Annotated[
            int,
            Field(
                description=(
                    "Maximum inline window bytes; default 16384, sized to the tool-result "
                    "token budget. The server caps the window at KDIVE_ARTIFACT_INLINE_MAX_BYTES "
                    "(default 65536); a larger artifact omits inline content — use "
                    "refs.download_uri for the whole object."
                )
            ),
        ] = artifact_reads.ARTIFACT_GET_WINDOW_DEFAULT_BYTES,
    ) -> ToolResponse:
        """Fetch a byte window of one redacted artifact's content by id.

        Returns the object ref plus, best-effort, a byte window of the redacted bytes
        inline in `data.content` (the window is `[byte_offset, byte_offset + max_bytes)`,
        capped at KDIVE_ARTIFACT_INLINE_MAX_BYTES). `data.content_truncated` and
        `data.next_offset` page the rest; an artifact above the fetch ceiling sets
        `content_omitted` and is retrieved via the always-present presigned
        `refs.download_uri`. Requires viewer; sensitive ids are not-found.
        """
        return await artifact_reads.artifacts_get(
            pool,
            current_context(),
            artifact_id=artifact_id,
            byte_offset=byte_offset,
            max_bytes=max_bytes,
        )


def _register_artifacts_fetch_raw(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="artifacts.fetch_raw",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def artifacts_fetch_raw(
        run_id: Annotated[str, Field(description="The Run whose raw asset to fetch.")],
        asset: Annotated[
            artifact_raw_fetch.RawAsset,
            Field(description="Which raw asset to fetch: vmcore or vmlinux."),
        ],
    ) -> ToolResponse:
        """Mint a presigned download URL for a Run's raw vmcore or vmlinux. Requires contributor.

        Returns the URL under `refs.download_uri` with `data.asset`/`data.size_bytes`; never
        inline bytes (these are large binaries). The asset stays sensitive — egress is gated by
        project membership + contributor on the asset's owning project, not by redaction.
        """
        return await artifact_raw_fetch.fetch_raw(
            pool, current_context(), run_id=run_id, asset=asset
        )


def _register_artifacts_search_text(app: FastMCP, pool: AsyncConnectionPool) -> None:
    read_handlers = artifact_reads.ArtifactReadHandlers()

    @app.tool(
        name="artifacts.search_text",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def artifacts_search_text(
        artifact_id: Annotated[str, Field(description="The redacted System artifact id.")],
        pattern: Annotated[
            str,
            Field(
                description=(
                    "Literal alternation pattern; '|' separates terms (grep-style), "
                    "e.g. '__d_lookup|panic'. The word 'OR' is not special."
                )
            ),
        ],
        before_lines: Annotated[
            int,
            Field(
                ge=BEFORE_LINES_RANGE[0],
                le=BEFORE_LINES_RANGE[1],
                description=(
                    f"Context lines before each match "
                    f"({BEFORE_LINES_RANGE[0]}–{BEFORE_LINES_RANGE[1]})."
                ),
            ),
        ] = 2,
        after_lines: Annotated[
            int,
            Field(
                ge=AFTER_LINES_RANGE[0],
                le=AFTER_LINES_RANGE[1],
                description=(
                    f"Context lines after each match "
                    f"({AFTER_LINES_RANGE[0]}–{AFTER_LINES_RANGE[1]})."
                ),
            ),
        ] = 4,
        max_matches: Annotated[
            int,
            Field(
                ge=MAX_MATCHES_RANGE[0],
                le=MAX_MATCHES_RANGE[1],
                description=(
                    f"Maximum match windows to return "
                    f"({MAX_MATCHES_RANGE[0]}–{MAX_MATCHES_RANGE[1]})."
                ),
            ),
        ] = 20,
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
            Field(
                description="Declared build artifacts: [{name, sha256 (base64), size_bytes}].",
                json_schema_extra=_declaration_schema_extra(
                    artifact_uploads.RUN_DECLARATION_EXAMPLES
                ),
            ),
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
            Field(
                description="Declared rootfs artifact: [{name, sha256 (base64), size_bytes}].",
                json_schema_extra=_declaration_schema_extra(
                    artifact_uploads.SYSTEM_DECLARATION_EXAMPLES
                ),
            ),
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
