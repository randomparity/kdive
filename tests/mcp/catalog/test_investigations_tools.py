"""investigations.* tool tests — handlers called directly with an injected pool + ctx."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import InvestigationState
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.catalog import investigations as inv_tools
from kdive.security.authz.rbac import AuthorizationError, Role
from tests.db_waits import wait_until_any_backend_waiting


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _open(pool: AsyncConnectionPool, ctx: RequestContext, **kw: Any):
    return await inv_tools.open_investigation(pool, ctx, **kw)


def test_open_mints_investigation_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _open(pool, _ctx(), project="proj", title="kernel oops in xfs")
            assert resp.status == "open"
            inv_id = resp.object_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, title FROM investigations WHERE id = %s", (inv_id,)
                )
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = '->open' "
                    "AND object_id = %s",
                    (inv_id,),
                )
                audit = await cur.fetchone()
        assert row is not None and row["state"] == "open" and row["title"] == "kernel oops in xfs"
        assert audit is not None and audit["n"] == 1

    asyncio.run(_run())


def test_open_keyed_retry_replays_one_investigation(migrated_url: str) -> None:
    """A keyed retry replays the identical envelope and mints exactly one Investigation."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            first = await _open(pool, _ctx(), project="proj", title="oops", idempotency_key="k1")
            second = await _open(pool, _ctx(), project="proj", title="oops", idempotency_key="k1")
            assert first.model_dump() == second.model_dump()
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM investigations")
                inv_n = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM idempotency_keys WHERE kind = 'investigations.open'"
                )
                key_n = await cur.fetchone()
        assert inv_n is not None and inv_n["n"] == 1
        assert key_n is not None and key_n["n"] == 1

    asyncio.run(_run())


def test_open_persists_and_dedups_external_refs(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            refs = [
                {"tracker": "bz", "id": "42", "url": "https://bz/42"},
                {"tracker": "bz", "id": "42", "url": "https://bz/42-dup"},  # same (tracker,id)
                {"tracker": "jira", "id": "K-1", "url": "https://jira/K-1"},
            ]
            resp = await _open(pool, _ctx(), project="proj", title="t", external_refs=refs)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT external_refs FROM investigations WHERE id = %s", (resp.object_id,)
                )
                row = await cur.fetchone()
        assert row is not None
        stored = {(r["tracker"], r["id"]): r["url"] for r in row["external_refs"]}
        assert stored == {("bz", "42"): "https://bz/42-dup", ("jira", "K-1"): "https://jira/K-1"}

    asyncio.run(_run())


def test_open_malformed_external_ref_is_config_error_no_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            bad = [{"tracker": "bz", "id": "42"}]  # missing url
            resp = await _open(pool, _ctx(), project="proj", title="t", external_refs=bad)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM investigations")
                n = await cur.fetchone()
        assert resp.status == "error" and resp.error_category == "configuration_error"
        # ADR-0174: a malformed external ref names its reason.
        assert resp.data["reason"] == "invalid_external_ref"
        assert resp.detail is not None
        assert n is not None and n["n"] == 0

    asyncio.run(_run())


def test_open_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            with pytest.raises(AuthorizationError):
                await _open(pool, _ctx(Role.VIEWER), project="proj", title="t")

    asyncio.run(_run())


def test_get_own_investigation_renders_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            resp = await inv_tools.get_investigation(pool, _ctx(), opened.object_id)
        assert resp.status == "open"
        assert resp.data["external_refs"] == []

    asyncio.run(_run())


def test_get_reports_title_and_description(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="xfs oops", description="hyp")
            resp = await inv_tools.get_investigation(pool, _ctx(), opened.object_id)
            assert resp.data["title"] == "xfs oops"
            assert resp.data["description"] == "hyp"
            assert resp.data["external_refs"] == []
            assert resp.data["state"] == "open"
            assert resp.data["last_run_at"] is None

    asyncio.run(scenario())


def test_get_investigation_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            with pytest.raises(AuthorizationError):
                await inv_tools.get_investigation(pool, _ctx(role=None), opened.object_id)

    asyncio.run(_run())


def test_get_cross_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            resp = await inv_tools.get_investigation(
                pool, _ctx(projects=("other",)), opened.object_id
            )
        assert resp.status == "error" and resp.error_category == "not_found"

    asyncio.run(_run())


def test_get_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await inv_tools.get_investigation(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error" and resp.error_category == "configuration_error"
        # ADR-0174: actionable reason + non-null detail for the malformed-id parse failure.
        assert resp.data["reason"] == "invalid_uuid"
        assert resp.detail is not None and "not-a-uuid" in resp.detail

    asyncio.run(_run())


async def _seed_investigation(pool: AsyncConnectionPool, state: InvestigationState) -> str:
    """Insert an Investigation directly in ``state`` (bypassing the open->… tools)."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from kdive.db.repositories import INVESTIGATIONS
    from kdive.domain.lifecycle import Investigation

    dt = datetime(2026, 1, 1, tzinfo=UTC)
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=dt,
                updated_at=dt,
                principal="user-1",
                project="proj",
                title="seeded",
                state=state,
            ),
        )
    return str(inv.id)


def test_close_open_investigation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.OPEN)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
            assert resp.status == "closed"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM investigations WHERE id = %s", (inv_id,))
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'open->closed' "
                    "AND object_id = %s",
                    (inv_id,),
                )
                audit = await cur.fetchone()
        assert row is not None and row["state"] == "closed"
        assert audit is not None and audit["n"] == 1

    asyncio.run(_run())


def test_close_active_investigation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.ACTIVE)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
        assert resp.status == "closed"

    asyncio.run(_run())


def test_close_already_closed_is_idempotent_no_audit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.CLOSED)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
            assert resp.status == "closed"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE object_id = %s", (inv_id,)
                )
                audit = await cur.fetchone()
        assert audit is not None and audit["n"] == 0  # no transition audited

    asyncio.run(_run())


def test_close_surfaces_enriched_envelope(migrated_url: str) -> None:
    """close renders the same enriched data as get (title/description/refs/state)."""

    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="xfs oops", description="hyp")
            resp = await inv_tools.close_investigation(pool, _ctx(), opened.object_id)
            assert resp.status == "closed"
            assert resp.data["title"] == "xfs oops"
            assert resp.data["description"] == "hyp"
            assert resp.data["external_refs"] == []
            assert resp.data["state"] == "closed"
            assert resp.suggested_next_actions == ["investigations.get"]
            # The idempotent already-closed path renders the same enriched envelope.
            again = await inv_tools.close_investigation(pool, _ctx(), opened.object_id)
            assert again.status == "closed"
            assert again.data["title"] == "xfs oops"
            assert again.data["description"] == "hyp"

    asyncio.run(scenario())


def test_close_abandoned_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.ABANDONED)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "abandoned"

    asyncio.run(_run())


def test_close_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.OPEN)
            with pytest.raises(AuthorizationError):
                await inv_tools.close_investigation(pool, _ctx(Role.VIEWER), inv_id)

    asyncio.run(_run())


def test_close_cross_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.OPEN)
            resp = await inv_tools.close_investigation(pool, _ctx(projects=("other",)), inv_id)
        assert resp.status == "error" and resp.error_category == "not_found"

    asyncio.run(_run())


def test_close_backstop_maps_illegal_transition(
    monkeypatch: pytest.MonkeyPatch, migrated_url: str
) -> None:
    # Force the IllegalTransition backstop: make update_state raise so the handler's
    # except-branch maps it to configuration_error rather than letting it escape.
    from kdive.db.repositories import INVESTIGATIONS
    from kdive.domain.capacity.state import IllegalTransition

    async def _boom(*_a: object, **_k: object) -> object:
        raise IllegalTransition("forced")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.OPEN)
            monkeypatch.setattr(INVESTIGATIONS, "update_state", _boom)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def _refs_of(pool: AsyncConnectionPool, inv_id: str):
    async def _q() -> dict[tuple[str, str], str]:
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT external_refs FROM investigations WHERE id = %s", (inv_id,))
            row = await cur.fetchone()
        assert row is not None
        return {(r["tracker"], r["id"]): r["url"] for r in row["external_refs"]}

    return _q


def test_link_then_unlink_round_trip(migrated_url: str) -> None:
    # The issue's first acceptance criterion: open -> link -> unlink mutates external_refs.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            ref: inv_tools.ExternalRefInput = {
                "tracker": "bz",
                "id": "7",
                "url": "https://bz/7",
            }
            await inv_tools.link_external_ref(pool, _ctx(), inv_id, ref)
            after_link = await _refs_of(pool, inv_id)()
            await inv_tools.unlink_external_ref(
                pool, _ctx(), inv_id, {"tracker": ref["tracker"], "id": ref["id"]}
            )
            after_unlink = await _refs_of(pool, inv_id)()
        assert after_link == {("bz", "7"): "https://bz/7"}
        assert after_unlink == {}

    asyncio.run(_run())


def test_link_upserts_changed_url(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            await inv_tools.link_external_ref(
                pool, _ctx(), inv_id, {"tracker": "bz", "id": "7", "url": "https://bz/7"}
            )
            await inv_tools.link_external_ref(
                pool, _ctx(), inv_id, {"tracker": "bz", "id": "7", "url": "https://bz/7-fixed"}
            )
            refs = await _refs_of(pool, inv_id)()
        assert refs == {("bz", "7"): "https://bz/7-fixed"}  # one entry, url corrected

    asyncio.run(_run())


def test_unlink_by_natural_key_without_url(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            await inv_tools.link_external_ref(
                pool, _ctx(), inv_id, {"tracker": "bz", "id": "7", "url": "https://bz/7"}
            )
            # No url unlinks the (bz,7) entry (matching ignores url).
            await inv_tools.unlink_external_ref(pool, _ctx(), inv_id, {"tracker": "bz", "id": "7"})
            refs = await _refs_of(pool, inv_id)()
        assert refs == {}

    asyncio.run(_run())


def test_unlink_absent_is_idempotent_no_audit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            resp = await inv_tools.unlink_external_ref(
                pool, _ctx(), inv_id, {"tracker": "bz", "id": "nope"}
            )
            assert resp.status == "open"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'unlink' "
                    "AND object_id = %s",
                    (inv_id,),
                )
                audit = await cur.fetchone()
        assert audit is not None and audit["n"] == 0

    asyncio.run(_run())


def test_unlink_malformed_ref_key_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            resp = await inv_tools.unlink_external_ref(pool, _ctx(), inv_id, {"tracker": "bz"})
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_external_ref"
        assert resp.detail is not None

    asyncio.run(_run())


def test_link_on_closed_investigation_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.CLOSED)
            resp = await inv_tools.link_external_ref(
                pool, _ctx(), inv_id, {"tracker": "bz", "id": "7", "url": "https://bz/7"}
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "closed"

    asyncio.run(_run())


def test_link_malformed_ref_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            resp = await inv_tools.link_external_ref(
                pool,
                _ctx(),
                inv_id,
                cast(inv_tools.ExternalRefInput, {"tracker": "bz"}),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_external_ref"
        assert resp.detail is not None

    asyncio.run(_run())


def test_link_acquires_investigation_lock(migrated_url: str) -> None:
    # Deterministic lock proof: hold the INVESTIGATION advisory lock on a separate
    # connection; the link must block until it is released.
    import psycopg

    from kdive.db.locks import LockScope, advisory_xact_lock

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            uid = UUID(inv_id)
            async with await psycopg.AsyncConnection.connect(migrated_url) as holder:
                async with (
                    holder.transaction(),
                    advisory_xact_lock(holder, LockScope.INVESTIGATION, uid),
                ):
                    task = asyncio.create_task(
                        inv_tools.link_external_ref(
                            pool,
                            _ctx(),
                            inv_id,
                            {"tracker": "bz", "id": "7", "url": "https://bz/7"},
                        )
                    )
                    await wait_until_any_backend_waiting(holder, locktype="advisory")
                    assert not task.done()  # blocked on the held INVESTIGATION lock
                # holder transaction committed here -> lock released
                resp = await task
            assert resp.status == "open"

    asyncio.run(_run())


def test_open_persists_description(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _open(pool, _ctx(), project="proj", title="t", description="oops in xfs")
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT description FROM investigations WHERE id = %s", (resp.object_id,)
                )
                row = await cur.fetchone()
            assert row is not None and row["description"] == "oops in xfs"

    asyncio.run(scenario())


def test_open_empty_description_stores_null(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _open(pool, _ctx(), project="proj", title="t", description="")
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT description FROM investigations WHERE id = %s", (resp.object_id,)
                )
                row = await cur.fetchone()
            assert row is not None and row["description"] is None

    asyncio.run(scenario())


def test_open_overlong_description_is_config_error(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _open(pool, _ctx(), project="proj", title="t", description="x" * 4097)
            assert resp.error_category == "configuration_error"

    asyncio.run(scenario())


def test_open_overlong_title_is_config_error(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _open(pool, _ctx(), project="proj", title="x" * 201)
            assert resp.error_category == "configuration_error"

    asyncio.run(scenario())


def test_set_updates_title_and_description(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="old")
            resp = await inv_tools.set_investigation(
                pool, _ctx(), opened.object_id, title="new", description="note"
            )
            assert resp.data["title"] == "new"
            assert resp.data["description"] == "note"

    asyncio.run(scenario())


def test_set_clear_description_with_empty_string(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t", description="x")
            resp = await inv_tools.set_investigation(pool, _ctx(), opened.object_id, description="")
            assert resp.data["description"] is None

    asyncio.run(scenario())


def test_set_omitting_description_leaves_it(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t", description="keep")
            resp = await inv_tools.set_investigation(
                pool, _ctx(), opened.object_id, title="renamed"
            )
            assert resp.data["description"] == "keep"
            assert resp.data["title"] == "renamed"

    asyncio.run(scenario())


def test_set_requires_at_least_one_field(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            resp = await inv_tools.set_investigation(pool, _ctx(), opened.object_id)
            assert resp.error_category == "configuration_error"
            # ADR-0174: an empty set payload names the missing-field reason.
            assert resp.data["reason"] == "missing_required_field"
            assert resp.detail is not None

    asyncio.run(scenario())


def test_set_overlong_title_is_config_error(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            resp = await inv_tools.set_investigation(
                pool, _ctx(), opened.object_id, title="x" * 201
            )
            assert resp.error_category == "configuration_error"
            # ADR-0174: an out-of-bounds title names the invalid-text reason.
            assert resp.data["reason"] == "invalid_text"
            assert resp.detail is not None

    asyncio.run(scenario())


def test_set_empty_title_is_config_error(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            resp = await inv_tools.set_investigation(pool, _ctx(), opened.object_id, title="")
            assert resp.error_category == "configuration_error"

    asyncio.run(scenario())


def test_set_on_closed_is_config_error(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            await inv_tools.close_investigation(pool, _ctx(), opened.object_id)
            resp = await inv_tools.set_investigation(pool, _ctx(), opened.object_id, title="new")
            assert resp.error_category == "configuration_error"
            assert resp.data["current_status"] == "closed"

    asyncio.run(scenario())


def test_set_requires_operator_role(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            with pytest.raises(AuthorizationError):
                await inv_tools.set_investigation(
                    pool, _ctx(Role.VIEWER), opened.object_id, title="new"
                )

    asyncio.run(scenario())


def test_set_audits_title_value_and_description_flag(migrated_url: str) -> None:
    """The audit digest covers the title value + a description flag, never the body."""

    async def scenario() -> None:
        from kdive.security.audit import args_digest

        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="old")
            await inv_tools.set_investigation(
                pool, _ctx(), opened.object_id, title="renamed", description="a secret note body"
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT args_digest FROM audit_log WHERE transition = 'set' AND object_id = %s",
                    (opened.object_id,),
                )
                row = await cur.fetchone()
        assert row is not None
        # Digest matches title value + the "set" flag — not the description body.
        assert row["args_digest"] == args_digest({"title": "renamed", "description": "set"})

    asyncio.run(scenario())


def test_set_reads_preexisting_overlong_title(migrated_url: str) -> None:
    """Finding-1 regression: a title written before the bound stays readable/editable."""

    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                # No id given — the column defaults to gen_random_uuid(); no uuid4 needed.
                await cur.execute(
                    "INSERT INTO investigations (title, state, principal, project) "
                    "VALUES (%s, 'open', 'p', 'proj') RETURNING id",
                    ("y" * 300,),
                )
                row = await cur.fetchone()
            assert row is not None
            inv_id = row["id"]
            resp = await inv_tools.get_investigation(pool, _ctx(), str(inv_id))
            assert resp.status == "open"  # read did not raise on the 300-char title

    asyncio.run(scenario())


_ATTACH_SEQ = itertools.count()


async def _attach_run(
    pool: AsyncConnectionPool,
    inv_id: str,
    *,
    system_id: str | None = None,
    project: str = "proj",
) -> tuple[str, str]:
    """Insert one Run on ``inv_id`` (minting an Allocation+System unless ``system_id`` given).

    Returns ``(run_id, system_id)``. Created-at is advanced per call so the ``created_at, id``
    ordering is deterministic across successive attaches.
    """
    from datetime import UTC, datetime
    from uuid import UUID, uuid4

    from kdive.db.repositories import RUNS
    from kdive.domain.capacity.state import RunState
    from kdive.domain.catalog.resources import ResourceKind
    from kdive.domain.lifecycle import Run
    from tests.mcp._seed import seed_crashed_system

    n = next(_ATTACH_SEQ)
    sid = system_id or await seed_crashed_system(pool, project=project)
    async with pool.connection() as conn:
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=datetime(2026, 1, 1, 0, 0, n, tzinfo=UTC),
                updated_at=datetime(2026, 1, 1, 0, 0, n, tzinfo=UTC),
                principal="user-1",
                project=project,
                investigation_id=UUID(inv_id),
                system_id=UUID(sid),
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=RunState.RUNNING,
                build_profile={},
            ),
        )
    return str(run.id), sid


def test_get_enumerates_attached_runs_and_systems(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            run1, sys1 = await _attach_run(pool, inv_id)
            run2, sys2 = await _attach_run(pool, inv_id)
            resp = await inv_tools.get_investigation(pool, _ctx(), inv_id)
        assert resp.data["runs"] == [run1, run2]  # created_at, id order
        assert resp.data["systems"] == [sys1, sys2]

    asyncio.run(scenario())


def test_get_with_no_runs_returns_empty_lists(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            resp = await inv_tools.get_investigation(pool, _ctx(), inv_id)
        assert resp.data["runs"] == []
        assert resp.data["systems"] == []

    asyncio.run(scenario())


def test_get_dedups_systems_across_runs(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            run1, sys1 = await _attach_run(pool, inv_id)
            run2, _ = await _attach_run(pool, inv_id, system_id=sys1)  # same System
            resp = await inv_tools.get_investigation(pool, _ctx(), inv_id)
        assert resp.data["runs"] == [run1, run2]
        assert resp.data["systems"] == [sys1]  # one distinct System

    asyncio.run(scenario())


def test_get_excludes_runs_of_another_investigation(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            inv_a = (await _open(pool, _ctx(), project="proj", title="a")).object_id
            inv_b = (await _open(pool, _ctx(), project="proj", title="b")).object_id
            run_a, _ = await _attach_run(pool, inv_a)
            await _attach_run(pool, inv_b)  # belongs to inv_b
            resp = await inv_tools.get_investigation(pool, _ctx(), inv_a)
        assert resp.data["runs"] == [run_a]  # only inv_a's run

    asyncio.run(scenario())


def test_open_envelope_carries_empty_runs(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _open(pool, _ctx(), project="proj", title="t")
        assert resp.data["runs"] == []
        assert resp.data["systems"] == []

    asyncio.run(scenario())


def test_close_envelope_enumerates_runs(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            run1, sys1 = await _attach_run(pool, inv_id)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
        assert resp.status == "closed"
        assert resp.data["runs"] == [run1]
        assert resp.data["systems"] == [sys1]

    asyncio.run(scenario())


def test_list_item_enumerates_runs(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="only")).object_id
            run1, sys1 = await _attach_run(pool, inv_id)
            resp = await inv_tools.list_investigations(pool, _ctx())
            assert len(resp.items) == 1
            item = resp.items[0]
        assert item.data["runs"] == [run1]
        assert item.data["systems"] == [sys1]

    asyncio.run(scenario())


def test_list_scopes_to_viewer_projects(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            await _open(pool, _ctx(), project="proj", title="a")
            await _open(pool, _ctx(), project="proj", title="b")
            resp = await inv_tools.list_investigations(pool, _ctx())
            assert resp.data["count"] == 2
            assert {i.data["title"] for i in resp.items} == {"a", "b"}

    asyncio.run(scenario())


def test_list_excludes_other_projects(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            await _open(pool, _ctx(), project="proj", title="mine")
            # A viewer of only "other" sees none of proj's investigations.
            resp = await inv_tools.list_investigations(pool, _ctx(projects=("other",)))
            assert resp.data["count"] == 0

    asyncio.run(scenario())


def test_list_state_filter(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="a")
            await _open(pool, _ctx(), project="proj", title="b")
            await inv_tools.close_investigation(pool, _ctx(), opened.object_id)
            resp = await inv_tools.list_investigations(pool, _ctx(), state="open")
            assert {i.data["title"] for i in resp.items} == {"b"}

    asyncio.run(scenario())


def test_list_bad_state_is_config_error(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            resp = await inv_tools.list_investigations(pool, _ctx(), state="nonsense")
            assert resp.error_category == "configuration_error"
            # ADR-0174: an unknown state filter enumerates the accepted Investigation states.
            assert resp.data["reason"] == "invalid_state"
            assert "open" in cast(list[str], resp.data["accepted_values"])

    asyncio.run(scenario())


def test_list_requires_viewer_role(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            await _open(pool, _ctx(), project="proj", title="a")
            # A caller with no viewer role anywhere sees an empty collection.
            resp = await inv_tools.list_investigations(pool, _ctx(role=None))
            assert resp.data["count"] == 0

    asyncio.run(scenario())


def test_investigation_row_error_envelope() -> None:
    from uuid import uuid4 as _u  # local import; module-level imports include only UUID

    resp = inv_tools._investigation_row_error(_u())
    assert resp.status == "error"
    assert resp.error_category == "configuration_error"


def test_list_degrades_one_invalid_row(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """One row failing model_validate degrades to an error item; the rest still render."""

    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            await _open(pool, _ctx(), project="proj", title="good-a")
            await _open(pool, _ctx(), project="proj", title="good-b")
            calls = {"n": 0}
            real = inv_tools.Investigation.model_validate

            def flaky(row: object) -> object:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ValueError("synthetic invalid row")
                return real(row)

            monkeypatch.setattr(inv_tools.Investigation, "model_validate", staticmethod(flaky))
            resp = await inv_tools.list_investigations(pool, _ctx())
            assert resp.data["count"] == 2
            assert sorted(i.status for i in resp.items) == ["error", "open"]

    asyncio.run(scenario())
