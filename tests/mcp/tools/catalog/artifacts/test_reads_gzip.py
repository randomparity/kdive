"""artifacts.get gzip decompress-on-read tests.

Verifies that ``artifacts.get`` inflates gzip-compressed artifact bodies before windowing
when ``head().content_encoding == "gzip"`` (Task 2 of #892). Detection is metadata-driven
only: an object whose head carries no ``content_encoding`` is served as raw bytes regardless
of whether its body looks like gzip.
"""

from __future__ import annotations

import asyncio
import gzip
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import FetchedArtifact, HeadResult
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.catalog.artifacts.reads import artifacts_get
from kdive.security.authz.rbac import Role
from tests.mcp._seed import seed_crashed_system
from tests.mcp.json_data import data_bool, data_int, data_str


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="u", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


class _GzipStore:
    """Object-store stub returning a gzip body with ``content_encoding="gzip"`` in head."""

    def __init__(
        self,
        body: bytes,
        *,
        sensitivity: Sensitivity = Sensitivity.REDACTED,
    ) -> None:
        self.body = body
        self.sensitivity = sensitivity

    def head(self, key: str) -> HeadResult | None:
        return HeadResult(
            size_bytes=len(self.body),
            checksum_sha256=None,
            etag="e",
            sensitivity=self.sensitivity,
            content_encoding="gzip",
        )

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        return FetchedArtifact(self.body, self.sensitivity, "console")

    def presign_get(self, key: str, *, expires_in: int) -> str:
        return f"https://store.example/{key}?token=stub"


class _PlainStore:
    """Object-store stub returning a plain body with no ``content_encoding`` in head."""

    def __init__(self, body: bytes, *, sensitivity: Sensitivity = Sensitivity.REDACTED) -> None:
        self.body = body
        self.sensitivity = sensitivity

    def head(self, key: str) -> HeadResult | None:
        return HeadResult(
            size_bytes=len(self.body),
            checksum_sha256=None,
            etag="e",
            sensitivity=self.sensitivity,
        )

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        return FetchedArtifact(self.body, self.sensitivity, "console")

    def presign_get(self, key: str, *, expires_in: int) -> str:
        return f"https://store.example/{key}?token=stub"


async def _seed_redacted_artifact(pool: AsyncConnectionPool) -> str:
    """Insert a System and one redacted console artifact owned by it; return artifact id."""
    sys_id = await seed_crashed_system(pool)
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES ('systems', %s, %s, 'e', 'redacted', 'console') RETURNING id",
            (sys_id, f"k/systems/{sys_id}/console-part"),
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row["id"])


def test_artifacts_get_inflates_gzip_part(migrated_url: str) -> None:
    """A gzip-compressed artifact is inflated before windowing."""
    plaintext = b"hello world\n" * 1000
    compressed = gzip.compress(plaintext)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            artifact_id = await _seed_redacted_artifact(pool)
            store = _GzipStore(compressed)
            resp = await artifacts_get(
                pool,
                _ctx(),
                artifact_id=artifact_id,
                byte_offset=0,
                max_bytes=64,
                store_factory=lambda: store,
            )
        assert resp.status == "available"
        assert data_str(resp, "content") == plaintext[:64].decode()
        assert data_bool(resp, "content_truncated") is True
        assert data_int(resp, "next_offset") == 64  # offset into plaintext, not compressed

    asyncio.run(_run())


def test_artifacts_get_gzip_size_bytes_reflects_inflated_length(migrated_url: str) -> None:
    """size_bytes in the response is the inflated plaintext length, not the compressed length."""
    plaintext = b"hello world\n" * 1000
    compressed = gzip.compress(plaintext)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            artifact_id = await _seed_redacted_artifact(pool)
            store = _GzipStore(compressed)
            resp = await artifacts_get(
                pool,
                _ctx(),
                artifact_id=artifact_id,
                byte_offset=0,
                max_bytes=len(plaintext),
                store_factory=lambda: store,
            )
        assert resp.status == "available"
        assert len(compressed) < len(plaintext)  # sanity: gzip actually compresses
        assert data_int(resp, "size_bytes") == len(plaintext)
        assert data_bool(resp, "content_truncated") is False
        assert "next_offset" not in resp.data

    asyncio.run(_run())


def test_artifacts_get_corrupt_gzip_degrades(migrated_url: str) -> None:
    """A corrupt gzip body degrades to content_unavailable='decode_error', never raises."""
    corrupt = b"this is not valid gzip data at all"

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            artifact_id = await _seed_redacted_artifact(pool)
            store = _GzipStore(corrupt)
            resp = await artifacts_get(
                pool,
                _ctx(),
                artifact_id=artifact_id,
                byte_offset=0,
                max_bytes=64,
                store_factory=lambda: store,
            )
        assert resp.status == "available"
        assert "content" not in resp.data
        assert data_str(resp, "content_unavailable") == "decode_error"

    asyncio.run(_run())


def test_artifacts_get_non_gzip_reads_byte_identically(migrated_url: str) -> None:
    """A plain (non-gzip) artifact reads byte-identically — regression guard."""
    body = b"plain text artifact content\n" * 10

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            artifact_id = await _seed_redacted_artifact(pool)
            store = _PlainStore(body)
            resp = await artifacts_get(
                pool,
                _ctx(),
                artifact_id=artifact_id,
                byte_offset=0,
                max_bytes=len(body),
                store_factory=lambda: store,
            )
        assert resp.status == "available"
        assert data_str(resp, "content") == body.decode()
        assert data_int(resp, "size_bytes") == len(body)
        assert data_bool(resp, "content_truncated") is False

    asyncio.run(_run())
