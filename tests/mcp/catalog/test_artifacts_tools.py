"""artifacts.* tool tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import FetchedArtifact, HeadResult
from kdive.domain.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog.artifacts.reads import (
    ArtifactReadHandlers,
    ArtifactSearchRequest,
    artifacts_get,
    artifacts_list,
)
from kdive.security.authz.rbac import AuthorizationError, Role
from tests.mcp._seed import seed_crashed_system
from tests.mcp.json_data import data_str


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


async def _seed_system_with_artifacts(pool: AsyncConnectionPool) -> tuple[str, str, str]:
    """Insert a System and a sensitive + redacted artifact owned by it.

    Returns (system_id, sensitive_artifact_id, redacted_artifact_id).
    """
    sys_id = await seed_crashed_system(pool)
    ids: list[str] = []
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        for name, sens in (("vmcore", "sensitive"), ("vmcore-redacted", "redacted")):
            await cur.execute(
                "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                "retention_class) VALUES ('systems', %s, %s, 'e', %s, 'vmcore') RETURNING id",
                (sys_id, f"k/systems/{sys_id}/{name}", sens),
            )
            row = await cur.fetchone()
            assert row is not None
            ids.append(str(row["id"]))
    return sys_id, ids[0], ids[1]


async def _seed_quarantined_artifact(pool: AsyncConnectionPool, sys_id: str) -> str:
    """Insert a quarantined artifact owned by an existing System; return its id."""
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES ('systems', %s, %s, 'e', 'quarantined', 'console') "
            "RETURNING id",
            (sys_id, f"k/systems/{sys_id}/console-quarantined"),
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row["id"])


class _SearchStore:
    def __init__(
        self,
        data: bytes,
        *,
        size: int | None = None,
        sensitivity: Sensitivity = Sensitivity.REDACTED,
        head_sensitivity: Sensitivity | None = Sensitivity.REDACTED,
        head_error: CategorizedError | None = None,
        get_error: CategorizedError | None = None,
        presign_error: CategorizedError | None = None,
        missing_head: bool = False,
    ) -> None:
        self.data = data
        self.size = len(data) if size is None else size
        self.sensitivity = sensitivity
        self.head_sensitivity = head_sensitivity
        self.head_error = head_error
        self.get_error = get_error
        self.presign_error = presign_error
        self.missing_head = missing_head
        self.headed = False
        self.got = False
        self.presigned_key: str | None = None
        self.presigned_expires_in: int | None = None

    def head(self, key: str) -> HeadResult | None:
        self.headed = True
        if self.head_error is not None:
            raise self.head_error
        if self.missing_head:
            return None
        return HeadResult(
            size_bytes=self.size,
            checksum_sha256=None,
            etag="e",
            sensitivity=self.head_sensitivity,
        )

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        self.got = True
        if self.get_error is not None:
            raise self.get_error
        assert etag == "e"
        return FetchedArtifact(self.data, self.sensitivity, "console")

    def presign_get(self, key: str, *, expires_in: int) -> str:
        self.presigned_key = key
        self.presigned_expires_in = expires_in
        if self.presign_error is not None:
            raise self.presign_error
        return f"https://store.example/{key}?token=stub"


def _artifact_read_handlers(store: _SearchStore) -> ArtifactReadHandlers:
    return ArtifactReadHandlers(lambda: store)


def _search_request(
    artifact_id: str,
    pattern: str,
    *,
    before_lines: int = 2,
    after_lines: int = 4,
    max_matches: int = 20,
) -> ArtifactSearchRequest:
    return ArtifactSearchRequest(
        artifact_id=artifact_id,
        pattern=pattern,
        before_lines=before_lines,
        after_lines=after_lines,
        max_matches=max_matches,
    )


def test_artifacts_search_text_maps_store_factory_failure() -> None:
    error = CategorizedError("store missing", category=ErrorCategory.CONFIGURATION_ERROR)

    def _raise_store() -> _SearchStore:
        raise error

    async def _run() -> ToolResponse:
        pool = AsyncConnectionPool("postgresql://unused", open=False)
        request = _search_request("artifact-1", "panic")
        return await ArtifactReadHandlers(_raise_store).artifacts_search_text(
            pool, _ctx(), request=request
        )

    resp = asyncio.run(_run())
    assert resp.object_id == "artifact-1"
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value


def test_artifacts_list_returns_redacted_only(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_list(pool, _ctx(), system_id=sys_id)
        ids = {r.object_id for r in resp.items}
        assert ids == {red_id}  # the sensitive row is never surfaced

    asyncio.run(_run())


def test_artifacts_search_text_returns_bounded_matches(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"before\nRIP: __d_lookup+0x1\nafter\n")
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool,
                _ctx(),
                request=_search_request(
                    red_id,
                    "__d_lookup|Oops",
                    before_lines=1,
                    after_lines=1,
                ),
            )
        assert resp.status == "searched"
        assert resp.data["match_count"] == "1"
        matches = json.loads(data_str(resp, "matches_json"))
        assert matches[0]["line"] == 2
        assert matches[0]["before"] == ["before"]
        assert matches[0]["after"] == ["after"]

    asyncio.run(_run())


def test_artifacts_search_text_sensitive_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, sens_id, _ = await _seed_system_with_artifacts(pool)
            resp = await _artifact_read_handlers(_SearchStore(b"panic")).artifacts_search_text(
                pool,
                _ctx(),
                request=_search_request(sens_id, "panic"),
            )
        assert resp.status == "error"
        assert resp.error_category == "not_found"

    asyncio.run(_run())


def test_artifacts_search_text_requires_viewer(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            with pytest.raises(AuthorizationError):
                await _artifact_read_handlers(_SearchStore(b"panic")).artifacts_search_text(
                    pool, _ctx(role=None), request=_search_request(red_id, "panic")
                )

    asyncio.run(_run())


def test_artifacts_search_text_rejects_oversized_before_get(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"", size=1024 * 1024 + 1)
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool, _ctx(), request=_search_request(red_id, "panic")
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "artifact_too_large"
        assert store.got is False

    asyncio.run(_run())


def test_artifacts_search_text_missing_store_head_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"panic", missing_head=True)
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool, _ctx(), request=_search_request(red_id, "panic")
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert store.got is False

    asyncio.run(_run())


def test_artifacts_search_text_maps_store_head_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(
                b"panic",
                head_error=CategorizedError(
                    "store down", category=ErrorCategory.INFRASTRUCTURE_FAILURE
                ),
            )
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool, _ctx(), request=_search_request(red_id, "panic")
            )
        assert resp.status == "error"
        assert resp.error_category == "infrastructure_failure"
        assert store.got is False

    asyncio.run(_run())


def test_artifacts_search_text_rejects_non_redacted_fetch(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"panic", sensitivity=Sensitivity.SENSITIVE)
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool, _ctx(), request=_search_request(red_id, "panic")
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert store.got is True

    asyncio.run(_run())


def test_artifacts_search_text_maps_store_get_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(
                b"panic",
                get_error=CategorizedError("stale", category=ErrorCategory.STALE_HANDLE),
            )
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool, _ctx(), request=_search_request(red_id, "panic")
            )
        assert resp.status == "error"
        assert resp.error_category == "stale_handle"

    asyncio.run(_run())


def test_artifacts_search_text_rejects_bad_pattern_before_head(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"panic")
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool, _ctx(), request=_search_request(red_id, "a||b")
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "bad_search_input"
        assert store.headed is False
        assert store.got is False

    asyncio.run(_run())


def test_artifacts_list_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, _ = await _seed_system_with_artifacts(pool)
            with pytest.raises(AuthorizationError):
                await artifacts_list(pool, _ctx(role=None), system_id=sys_id)

    asyncio.run(_run())


def test_artifacts_get_redacted_returns_ref(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_get(pool, _ctx(), artifact_id=red_id)
        assert resp.status != "error" and resp.refs

    asyncio.run(_run())


def test_artifacts_get_inlines_small_redacted_content(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"panic: redacted log\n")
            resp = await artifacts_get(
                pool, _ctx(), artifact_id=red_id, store_factory=lambda: store
            )
        assert resp.status == "available"
        assert data_str(resp, "content") == "panic: redacted log\n"
        assert data_str(resp, "content_truncated") == "false"
        assert data_str(resp, "size_bytes") == str(len(b"panic: redacted log\n"))
        assert resp.refs["download_uri"].endswith("?token=stub")
        # default TTL reaches presign_get with no env set
        assert store.presigned_expires_in == 900

    asyncio.run(_run())


def test_artifacts_get_omits_oversized_content_keeps_uri(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"", size=64 * 1024 + 1)
            resp = await artifacts_get(
                pool, _ctx(), artifact_id=red_id, store_factory=lambda: store
            )
        assert resp.status == "available"
        assert "content" not in resp.data
        assert data_str(resp, "content_omitted") == "artifact_too_large"
        assert resp.refs["download_uri"].endswith("?token=stub")
        assert store.got is False  # never fetched the oversized body

    asyncio.run(_run())


def test_artifacts_get_rejects_non_redacted_fetch(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"panic", sensitivity=Sensitivity.SENSITIVE)
            resp = await artifacts_get(
                pool, _ctx(), artifact_id=red_id, store_factory=lambda: store
            )
        # The redaction gate: a sensitive object at a redacted row's key is not-found-shaped.
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert store.got is True

    asyncio.run(_run())


def test_artifacts_get_rejects_non_redacted_head_before_uri(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            # Object metadata says sensitive though the DB row says redacted (drift):
            # the URI must NOT be minted and the body must NOT be fetched.
            store = _SearchStore(b"panic", head_sensitivity=Sensitivity.SENSITIVE)
            resp = await artifacts_get(
                pool, _ctx(), artifact_id=red_id, store_factory=lambda: store
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert store.presigned_key is None  # URI never minted
        assert store.got is False  # body never fetched

    asyncio.run(_run())


def test_artifacts_get_oversized_honors_head_redaction_gate(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            # An oversized object whose metadata says sensitive: no URI, not-found-shaped.
            store = _SearchStore(b"", size=64 * 1024 + 1, head_sensitivity=Sensitivity.SENSITIVE)
            resp = await artifacts_get(
                pool, _ctx(), artifact_id=red_id, store_factory=lambda: store
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert store.presigned_key is None
        assert store.got is False

    asyncio.run(_run())


def test_artifacts_get_degrades_when_store_unconfigured(migrated_url: str) -> None:
    error = CategorizedError("S3 unset", category=ErrorCategory.CONFIGURATION_ERROR)

    def _raise_store() -> _SearchStore:
        raise error

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_get(pool, _ctx(), artifact_id=red_id, store_factory=_raise_store)
        # Metadata envelope still returns; only the content/URI enrichment degrades.
        assert resp.status == "available"
        assert resp.refs["object"]
        assert "download_uri" not in resp.refs
        assert "content" not in resp.data
        assert data_str(resp, "content_unavailable") == "store_unconfigured"

    asyncio.run(_run())


def test_artifacts_get_degrades_on_store_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(
                b"panic",
                head_error=CategorizedError(
                    "store down", category=ErrorCategory.INFRASTRUCTURE_FAILURE
                ),
            )
            resp = await artifacts_get(
                pool, _ctx(), artifact_id=red_id, store_factory=lambda: store
            )
        assert resp.status == "available"
        assert resp.refs["object"]
        assert "download_uri" not in resp.refs
        assert "content" not in resp.data
        assert data_str(resp, "content_unavailable") == "store_error"

    asyncio.run(_run())


def test_artifacts_get_degrades_on_presign_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(
                b"panic",
                presign_error=CategorizedError(
                    "presign down", category=ErrorCategory.INFRASTRUCTURE_FAILURE
                ),
            )
            resp = await artifacts_get(
                pool, _ctx(), artifact_id=red_id, store_factory=lambda: store
            )
        assert resp.status == "available"
        assert "download_uri" not in resp.refs
        assert "content" not in resp.data
        assert data_str(resp, "content_unavailable") == "store_error"

    asyncio.run(_run())


def test_artifacts_get_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            with pytest.raises(AuthorizationError):
                await artifacts_get(pool, _ctx(role=None), artifact_id=red_id)

    asyncio.run(_run())


def test_artifacts_get_sensitive_is_not_found_shaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, sens_id, _ = await _seed_system_with_artifacts(pool)
            resp = await artifacts_get(pool, _ctx(), artifact_id=sens_id)
        assert resp.status == "error" and resp.error_category == "not_found"

    asyncio.run(_run())


def test_artifacts_get_cross_project_is_not_found_shaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_get(pool, _ctx(projects=("other",)), artifact_id=red_id)
        assert resp.status == "error" and resp.error_category == "not_found"

    asyncio.run(_run())


def test_artifacts_get_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await artifacts_get(pool, _ctx(), artifact_id="nope")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_artifacts_list_cross_project_is_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, _ = await _seed_system_with_artifacts(pool)
            resp = await artifacts_list(pool, _ctx(projects=("other",)), system_id=sys_id)
        assert resp.items == []

    asyncio.run(_run())


def test_artifacts_list_malformed_system_id_is_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await artifacts_list(pool, _ctx(), system_id="not-a-uuid")
        assert resp.items == []

    asyncio.run(_run())


def test_artifacts_get_excludes_quarantined(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, red_id = await _seed_system_with_artifacts(pool)
            quar_id = await _seed_quarantined_artifact(pool, sys_id)
            quar_resp = await artifacts_get(pool, _ctx(), artifact_id=quar_id)
            red_resp = await artifacts_get(pool, _ctx(), artifact_id=red_id)
        # Positive control: a redacted artifact in the same DB state IS served, so the
        # quarantined error is specifically the sensitivity gate, not a not-found/authz miss.
        assert red_resp.status == "available"
        assert quar_resp.status == "error"
        assert quar_resp.error_category == "not_found"

    asyncio.run(_run())


def test_artifacts_list_excludes_quarantined(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, red_id = await _seed_system_with_artifacts(pool)
            quar_id = await _seed_quarantined_artifact(pool, sys_id)
            resp = await artifacts_list(pool, _ctx(), system_id=sys_id)
        ids = {r.object_id for r in resp.items}
        assert quar_id not in ids
        assert red_id in ids

    asyncio.run(_run())


def test_artifacts_search_text_quarantined_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, _ = await _seed_system_with_artifacts(pool)
            quar_id = await _seed_quarantined_artifact(pool, sys_id)
            store = _SearchStore(b"panic")
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool, _ctx(), request=_search_request(quar_id, "panic")
            )
        assert resp.status == "error"
        assert resp.error_category == "not_found"
        assert store.got is False  # excluded by SQL before any object fetch

    asyncio.run(_run())
