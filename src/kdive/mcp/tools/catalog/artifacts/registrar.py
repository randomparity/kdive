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
from kdive.mcp.tools.catalog.artifacts.feature_requirements import (
    feature_config_requirements as _feature_config_requirements,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.artifacts.artifact_jump import JumpDirection
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
    _register_artifacts_find(app, pool)
    _register_artifacts_fetch_raw(app, pool)
    _register_artifacts_create_run_upload(app, pool, resolver)
    _register_artifacts_create_system_upload(app, pool, resolver)
    _register_artifacts_expected_uploads(app)
    _register_artifacts_feature_config_requirements(app)


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
        """List a System's redacted artifacts. Requires viewer.

        This listing is **System-scoped**: it returns every redacted artifact owned by the System
        and so mixes all of the System's Runs and debug sessions. Console artifacts use two naming
        conventions — `console-<run_id>` is a Run's one-time boot-window snapshot, and
        `console-part-<gen>-<index>` are the rotating post-readiness console parts. Neither is
        correlated to a Run by this listing; to get the console artifacts for a specific Run, call
        `runs.get` with `include_console_artifacts=true` and read its opt-in
        `data.console_artifacts` (the Run-scoped console manifest) instead.
        """
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
                    "Start byte of the window (0-based). With direction=forward (default) a "
                    "negative value reads from the start; with direction=backward the window runs "
                    "up from this byte and an omitted (0) or negative value means end-of-artifact. "
                    "Page with the returned data.next_offset until data.content_truncated is false."
                )
            ),
        ] = 0,
        max_bytes: Annotated[
            int,
            Field(
                description=(
                    "Maximum inline window bytes; default "
                    f"{artifact_reads.ARTIFACT_GET_WINDOW_DEFAULT_BYTES}, sized to the "
                    "tool-result token budget. The server caps the window at the smaller of a "
                    f"hard {artifact_reads.ARTIFACT_GET_WINDOW_MAX_BYTES}-byte token-safe ceiling "
                    "and KDIVE_ARTIFACT_INLINE_MAX_BYTES, so a larger value still returns at most "
                    f"{artifact_reads.ARTIFACT_GET_WINDOW_MAX_BYTES} bytes with data.next_offset "
                    "to page the rest; an artifact above the fetch ceiling omits inline content "
                    "— use refs.download_uri for the whole object when present. Store or "
                    "redaction failures set data.content_unavailable and omit download_uri."
                )
            ),
        ] = artifact_reads.ARTIFACT_GET_WINDOW_DEFAULT_BYTES,
        direction: Annotated[
            JumpDirection,
            Field(
                description=(
                    "Cursor direction for paging. forward starts at byte_offset (the artifact "
                    "start when omitted). backward starts at end-of-artifact when byte_offset is "
                    "omitted (read the tail and page up); a positive byte_offset bounds it."
                )
            ),
        ] = "forward",
    ) -> ToolResponse:
        """Fetch a byte window of one redacted artifact.

        Returns the object ref plus a byte window of the redacted bytes inline in
        `data.content` (`[byte_offset, byte_offset + max_bytes)`, capped at a hard token-safe
        ceiling and KDIVE_ARTIFACT_INLINE_MAX_BYTES); `data.content_truncated` and
        `data.next_offset` page the rest, in `direction` (forward from the start, or backward
        from the tail). An artifact above the fetch ceiling sets `content_omitted` and is
        retrieved via presigned `refs.download_uri` when the store is reachable and redaction
        checks pass. When the response sets `data.content_unavailable`, callers must handle the
        degraded result without a `download_uri`. Use `artifacts.find` for literal search.
        Requires viewer; sensitive ids are not-found.
        """
        return await artifact_reads.artifacts_get(
            pool,
            current_context(),
            artifact_id=artifact_id,
            byte_offset=byte_offset,
            max_bytes=max_bytes,
            direction=direction,
        )


def _register_artifacts_find(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="artifacts.find",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def artifacts_find(
        artifact_id: Annotated[
            str,
            Field(description="The redacted artifact to search (sensitive ids are not-found)."),
        ],
        query: Annotated[
            str,
            Field(
                description=(
                    "Literal search terms. '|' separates alternatives (e.g. "
                    "'BUG: KASAN|Call Trace'); the nearest term in direction is returned with "
                    "data.match_offset, data.match_line, the surrounding data.content, and "
                    "data.next_offset to continue (data.match_found is false when none remain). "
                    "Per-line literal substring, case-sensitive, no regex and no Unicode "
                    "normalization — match the artifact's exact bytes (kernel signatures are "
                    "ASCII)."
                )
            ),
        ],
        byte_offset: Annotated[
            int,
            Field(
                description=(
                    "Start byte for the search cursor. With direction=forward (default), search "
                    "starts at this byte and a negative value starts from the beginning. With "
                    "direction=backward, search runs up from this byte and an omitted (0) or "
                    "negative value means end-of-artifact."
                )
            ),
        ] = 0,
        max_bytes: Annotated[
            int,
            Field(
                description=(
                    "Maximum surrounding content bytes on a match; default "
                    f"{artifact_reads.ARTIFACT_GET_WINDOW_DEFAULT_BYTES}. The server caps this "
                    "at the smaller of a hard "
                    f"{artifact_reads.ARTIFACT_GET_WINDOW_MAX_BYTES}-byte token-safe ceiling and "
                    "KDIVE_ARTIFACT_INLINE_MAX_BYTES."
                )
            ),
        ] = artifact_reads.ARTIFACT_GET_WINDOW_DEFAULT_BYTES,
        direction: Annotated[
            JumpDirection,
            Field(
                description=(
                    "Search direction. forward starts at byte_offset (the artifact start when "
                    "omitted). backward starts at end-of-artifact when byte_offset is omitted; a "
                    "positive byte_offset bounds it."
                )
            ),
        ] = "forward",
    ) -> ToolResponse:
        """Jump to the nearest literal match in one redacted artifact.

        Returns `data.match_found` plus, on a hit, `data.match_offset`/`data.match_line`, the
        surrounding `data.content`, and `data.next_offset` to continue in `direction`. An
        artifact above the fetch ceiling cannot be searched and returns configuration_error
        with `data.reason=artifact_too_large`, not an empty match. Requires viewer; sensitive
        ids are not-found.
        """
        return await artifact_reads.artifacts_find(
            pool,
            current_context(),
            artifact_id=artifact_id,
            query=query,
            byte_offset=byte_offset,
            max_bytes=max_bytes,
            direction=direction,
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
        """Mint presigned PUTs for an external Run's build artifacts.

        Each upload item returns `refs.upload_url` plus `data.required_headers`; the client
        must send every required header on the PUT. Each call replaces the Run upload manifest,
        so corrections must redeclare every artifact that should remain part of the build.
        """
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
        """Mint a presigned PUT for a DEFINED System's rootfs. Requires contributor on the
        System's project."""
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


def _register_artifacts_feature_config_requirements(app: FastMCP) -> None:
    @app.tool(
        name="artifacts.feature_config_requirements",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def artifacts_feature_config_requirements() -> ToolResponse:
        """Advisory map of each debug/platform feature to the kernel ``CONFIG_*`` it needs.

        Read this before building a kernel to upload. Each ``data.features`` entry lists the
        ``feature``, a ``summary``, ``gated`` (whether kdive refuses to arm it without the
        config), and ``requirements`` (OR-groups of ``CONFIG_*`` — any symbol in a group
        satisfies it). Advisory only: kdive never validates your config; skip any feature you do
        not need. Requires a token.
        """
        # Auth-only (ADR-0117), like artifacts.expected_uploads: the manifest is a static public
        # vocabulary, so enforce token presence only — no platform/project gate, no audit.
        current_context()
        return _feature_config_requirements()
