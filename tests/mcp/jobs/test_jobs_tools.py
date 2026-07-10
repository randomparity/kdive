"""jobs.* handler tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import JobState
from kdive.domain.operations.jobs import (
    CONTRIBUTOR_CANCELABLE_JOB_KINDS,
    DESTRUCTIVE_JOB_KINDS,
    JobKind,
)
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, BuildPayload, SystemPayload
from kdive.mcp.auth import RequestContext
from kdive.mcp.middleware.denial_audit import DenialAuditMiddleware
from kdive.mcp.tools import jobs as jobs_tools
from kdive.security.audit import args_digest
from kdive.security.authz.rbac import Role, RoleDenied

WORKER_LOCAL_ID = "00000000-0000-0000-0000-0000000000c0"  # was db.build_hosts.WORKER_LOCAL_ID


CTX = RequestContext(principal="user-1", agent_session="s", projects=("proj",))
OP_CTX = RequestContext(
    principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.OPERATOR}
)
CONTRIB_CTX = RequestContext(
    principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.CONTRIBUTOR}
)
VIEWER_CTX = RequestContext(
    principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.VIEWER}
)


class _FakeMessage:
    def __init__(self, name: str, arguments: dict[str, object] | None = None) -> None:
        self.name = name
        self.arguments = arguments


class _FakeContext:
    def __init__(self, tool: str, arguments: dict[str, object] | None = None) -> None:
        self.message = _FakeMessage(tool, arguments)


def _build_payload() -> BuildPayload:
    return BuildPayload(run_id=str(uuid4()), build_host_id=str(WORKER_LOCAL_ID))


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _enqueue_in(pool: AsyncConnectionPool, dedup: str, project: str) -> str:
    """Enqueue a job whose authorizing tuple is owned by ``project``."""
    async with pool.connection() as conn:
        job = await queue.enqueue(
            conn,
            JobKind.BUILD,
            _build_payload(),
            Authorizing(principal="p", project=project),
            dedup,
        )
    return str(job.id)


async def _enqueue(pool: AsyncConnectionPool, dedup: str) -> str:
    """Enqueue a job in ``CTX``'s project (the common case for these tests)."""
    return await _enqueue_in(pool, dedup, "proj")


async def _enqueue_system_job(pool: AsyncConnectionPool, kind: JobKind, dedup: str) -> str:
    """Enqueue a SystemPayload job of ``kind`` (provision/force_crash) owned by ``proj``."""
    async with pool.connection() as conn:
        job = await queue.enqueue(
            conn,
            kind,
            SystemPayload(system_id=str(uuid4())),
            Authorizing(principal="p", project="proj"),
            dedup,
        )
    return str(job.id)


async def _mark_failed_without_category(pool: AsyncConnectionPool, job_id: str) -> None:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE jobs SET state = 'failed', error_category = NULL WHERE id = %s",
            (job_id,),
        )


async def _mark_running(pool: AsyncConnectionPool, job_id: str) -> None:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("UPDATE jobs SET state = 'running' WHERE id = %s", (job_id,))


async def _audit_rows(pool: AsyncConnectionPool, job_id: str) -> list[tuple[Any, ...]]:
    """Return every audit_log row for ``job_id`` (readable columns, newest first)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT principal, tool, object_kind, object_id::text, project, "
            "transition, args_digest FROM audit_log WHERE object_id = %s "
            "ORDER BY ts DESC",
            (job_id,),
        )
        return await cur.fetchall()


def test_get_known_job_returns_status(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            resp = await jobs_tools.get_job(pool, VIEWER_CTX, job_id)
        assert resp.object_id == job_id
        assert resp.status == "queued"
        assert resp.data == {"kind": "build"}

    asyncio.run(_run())


def test_get_unknown_job_is_error_envelope(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await jobs_tools.get_job(pool, CTX, str(uuid4()))
        assert resp.status == "error"
        assert resp.error_category == "not_found"

    asyncio.run(_run())


def test_get_job_degrades_invariant_violating_row(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "bad-get")
            await _mark_failed_without_category(pool, job_id)
            caplog.set_level(logging.WARNING, logger=jobs_tools.__name__)
            resp = await jobs_tools.get_job(pool, VIEWER_CTX, job_id)
        assert resp.object_id == job_id
        assert resp.status == "error"
        assert resp.error_category == "infrastructure_failure"
        assert any(
            record.exc_info is not None and f"job {job_id}" in record.message
            for record in caplog.records
        )

    asyncio.run(_run())


def test_get_malformed_id_is_error_envelope(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await jobs_tools.get_job(pool, CTX, "not-a-uuid")
        assert resp.object_id == "not-a-uuid"
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_cancel_queued_job_transitions(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            resp = await jobs_tools.cancel_job(pool, OP_CTX, job_id)
        assert resp.status == "canceled"

    asyncio.run(_run())


def test_cancel_job_contributor_can_cancel(migrated_url: str) -> None:
    # Leaseholder-control (#1080, ADR-0320): cancelling your own leaseholder-kind job is
    # contributor, matching runs.cancel. A contributor cancels a queued build job they own.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            resp = await jobs_tools.cancel_job(pool, CONTRIB_CTX, job_id)
        assert resp.status == "canceled"

    asyncio.run(_run())


@pytest.mark.parametrize("kind", [JobKind.FORCE_CRASH, JobKind.PROVISION])
def test_cancel_operator_gated_job_denied_to_contributor(migrated_url: str, kind: JobKind) -> None:
    # Per-kind gate (#1080, ADR-0320): a destructive job (force_crash) and the operator-gated
    # provision lane both keep the operator gate, so a contributor cannot veto an operator's op.
    # PROVISION in particular is deliberately out of #1080's scope (the provision-lane RBAC
    # review). The cancel must not land: the job stays queued.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue_system_job(pool, kind, f"opgate-{kind.value}")
            with pytest.raises(RoleDenied) as excinfo:
                await jobs_tools.cancel_job(pool, CONTRIB_CTX, job_id)
            owned = await jobs_tools.get_job(pool, VIEWER_CTX, job_id)
        assert excinfo.value.held is Role.CONTRIBUTOR
        assert excinfo.value.required is Role.OPERATOR
        assert owned.status == "queued"

    asyncio.run(_run())


def test_cancel_operator_gated_job_allowed_to_operator(migrated_url: str) -> None:
    # An operator may still cancel an operator-gated job — the pre-#1080 gate is preserved, so
    # #1080 does not change who may cancel provision/destructive jobs.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue_system_job(pool, JobKind.FORCE_CRASH, "opgate-op")
            resp = await jobs_tools.cancel_job(pool, OP_CTX, job_id)
        assert resp.status == "canceled"

    asyncio.run(_run())


def test_cancel_role_classification_covers_every_kind_and_fails_closed() -> None:
    # Guard the per-kind cancel gate against fail-open drift: every JobKind maps to contributor
    # or operator, and every kind outside the contributor allowlist (all destructive kinds and
    # the operator-gated provision lane) must fail closed to operator — so a newly added
    # privileged kind is never silently contributor-cancellable.
    for kind in JobKind:
        role = jobs_tools._cancel_role(kind)
        assert role in (Role.CONTRIBUTOR, Role.OPERATOR), kind
        if kind not in CONTRIBUTOR_CANCELABLE_JOB_KINDS:
            assert role is Role.OPERATOR, kind
    assert not CONTRIBUTOR_CANCELABLE_JOB_KINDS & DESTRUCTIVE_JOB_KINDS  # destructive never lowered
    assert JobKind.PROVISION not in CONTRIBUTOR_CANCELABLE_JOB_KINDS  # provision lane out of scope


def test_cancel_terminal_job_is_error_envelope(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            await jobs_tools.cancel_job(pool, OP_CTX, job_id)  # -> canceled (terminal)
            resp = await jobs_tools.cancel_job(pool, OP_CTX, job_id)  # again
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        # The agent learns the current state without a second jobs.get.
        assert resp.data == {"current_status": "canceled"}

    asyncio.run(_run())


def test_cancel_running_leaseholder_job_writes_audit_row(migrated_url: str) -> None:
    # #1083: a successful cancel writes one readable audit row attributing the actor, tool,
    # object, project, and a transition that names the job kind in plaintext (not only in the
    # one-way args_digest). BUILD is a contributor-cancelable leaseholder kind.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "audit-run")
            await _mark_running(pool, job_id)
            resp = await jobs_tools.cancel_job(pool, CONTRIB_CTX, job_id)
            rows = await _audit_rows(pool, job_id)
        assert resp.status == "canceled"
        assert len(rows) == 1
        principal, tool, object_kind, object_id, project, transition, digest = rows[0]
        assert principal == "user-1"
        assert tool == "jobs.cancel"
        assert object_kind == "jobs"
        assert object_id == job_id
        assert project == "proj"
        assert transition == "build:running->canceled"
        assert digest == args_digest({"job_id": job_id, "kind": "build"})

    asyncio.run(_run())


def test_cancel_queued_destructive_job_by_operator_writes_audit_row(migrated_url: str) -> None:
    # #1083: an operator cancelling a queued destructive-kind job is the cross-principal action
    # that most needs attribution. force_crash is the proven kind/payload pairing used by
    # test_cancel_operator_gated_job_allowed_to_operator.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue_system_job(pool, JobKind.FORCE_CRASH, "audit-fc")
            resp = await jobs_tools.cancel_job(pool, OP_CTX, job_id)
            rows = await _audit_rows(pool, job_id)
        assert resp.status == "canceled"
        assert len(rows) == 1
        assert rows[0][5] == "force_crash:queued->canceled"
        assert rows[0][6] == args_digest({"job_id": job_id, "kind": "force_crash"})

    asyncio.run(_run())


def test_cancel_audits_locked_prior_state_not_stale_read(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #1083 review: prior_state must come from the FOR UPDATE read inside the cancel transaction,
    # not the pre-authz read. queued<->running are both legal non-terminal edges, so a worker
    # claim landing between the two reads leaves the cancel legal yet would mislabel the prior
    # state. Simulate that skew: the pre-authz JOBS.get returns a stale `queued` snapshot while
    # the DB row is really `running`. The audit must label the real (running) state.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "audit-stale")
            await _mark_running(pool, job_id)  # DB row is now running
            real_get = jobs_tools.JOBS.get

            async def _stale_get(conn: Any, key: Any) -> Any:
                job = await real_get(conn, key)
                if job is not None and str(job.id) == job_id:
                    return job.model_copy(update={"state": JobState.QUEUED})
                return job

            monkeypatch.setattr(jobs_tools.JOBS, "get", _stale_get)
            resp = await jobs_tools.cancel_job(pool, CONTRIB_CTX, job_id)
            monkeypatch.undo()  # restore before reading the audit row
            rows = await _audit_rows(pool, job_id)
        assert resp.status == "canceled"
        assert len(rows) == 1
        # Labeled from the locked real state (running), not the stale pre-read (queued).
        assert rows[0][5] == "build:running->canceled"

    asyncio.run(_run())


def test_cancel_audit_failure_rolls_back_the_mutation(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0028 / #1083: the audit row and the ->canceled mutation commit atomically inside one
    # transaction. If audit.record raises after update_state, the whole transaction rolls back:
    # the job is NOT left canceled and no audit row survives. Guards the invariant against a
    # future refactor that pulls audit.record out of the transaction.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "audit-rollback")
            await _mark_running(pool, job_id)

            async def _boom(*_args: Any, **_kwargs: Any) -> Any:
                raise RuntimeError("audit sink down")

            monkeypatch.setattr(jobs_tools.audit, "record", _boom)
            with pytest.raises(RuntimeError, match="audit sink down"):
                await jobs_tools.cancel_job(pool, CONTRIB_CTX, job_id)
            monkeypatch.undo()
            resp = await jobs_tools.get_job(pool, VIEWER_CTX, job_id)
            rows = await _audit_rows(pool, job_id)
        assert resp.status == "running"  # rolled back with the audit failure, not canceled
        assert rows == []

    asyncio.run(_run())


def test_cancel_terminal_job_writes_no_audit_row(migrated_url: str) -> None:
    # #1083 / spec D3: an audit event records a transition. A no-op cancel of an already-terminal
    # job performs none, so it writes nothing — only the first, real cancel is attributed.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "audit-terminal")
            await jobs_tools.cancel_job(pool, OP_CTX, job_id)  # -> canceled (one row)
            resp = await jobs_tools.cancel_job(pool, OP_CTX, job_id)  # no-op, no row
            rows = await _audit_rows(pool, job_id)
        assert resp.status == "error"
        assert resp.data == {"current_status": "canceled"}
        assert len(rows) == 1  # exactly the first cancel, not the no-op

    asyncio.run(_run())


def test_cancel_denied_by_role_writes_no_audit_row(migrated_url: str) -> None:
    # #1083: cancel_job re-raises RoleDenied before mutating, so it writes no row of its own;
    # denial auditing stays the DenialAuditMiddleware's job (unchanged, out of this handler).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "audit-denied")
            with pytest.raises(RoleDenied):
                await jobs_tools.cancel_job(pool, VIEWER_CTX, job_id)
            rows = await _audit_rows(pool, job_id)
        assert rows == []

    asyncio.run(_run())


def test_wait_returns_immediately_for_terminal(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            await jobs_tools.cancel_job(pool, OP_CTX, job_id)
            resp = await jobs_tools.wait_job(pool, VIEWER_CTX, job_id, timeout_s=5.0)
        assert resp.status == "canceled"

    asyncio.run(_run())


def test_wait_zero_timeout_is_single_read(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")  # stays queued (no worker)
            resp = await jobs_tools.wait_job(pool, VIEWER_CTX, job_id, timeout_s=0.0)
        assert resp.status == "queued"  # one read, no wait

    asyncio.run(_run())


def test_wait_job_degrades_invariant_violating_terminal_row(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "bad-wait")
            await _mark_failed_without_category(pool, job_id)
            caplog.set_level(logging.WARNING, logger=jobs_tools.__name__)
            resp = await jobs_tools.wait_job(pool, VIEWER_CTX, job_id, timeout_s=0.0)
        assert resp.object_id == job_id
        assert resp.status == "error"
        assert resp.error_category == "infrastructure_failure"
        assert any(
            record.exc_info is not None and f"job {job_id}" in record.message
            for record in caplog.records
        )

    asyncio.run(_run())


def test_wait_caps_sleep_to_remaining_timeout(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")  # stays queued (no worker)
            sleeps: list[float] = []

            async def _sleep(delay: float) -> None:
                sleeps.append(delay)
                assert delay <= 0.05
                await asyncio.sleep(delay)

            resp = await jobs_tools.wait_job(
                pool,
                VIEWER_CTX,
                job_id,
                timeout_s=0.05,
                sleep=_sleep,
            )
        assert resp.status == "queued"
        assert len(sleeps) == 1

    asyncio.run(_run())


def test_wait_nonterminal_returns_promptly_with_call_again_signal(migrated_url: str) -> None:
    """A non-terminal wait returns at its clamped deadline (not held to MAX_WAIT_S) as a
    "still running, call again" signal: the non-terminal envelope carries `jobs.wait` in
    `suggested_next_actions` so an agent re-polls instead of holding one long idle stream
    (ADR-0138). The injected sleep keeps this wall-clock-free; it asserts the loop never
    sleeps past the tiny remaining timeout.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")  # stays queued (no worker)

            async def _sleep(delay: float) -> None:
                assert delay <= 0.01, "the loop must cap its sleep to the tiny remaining timeout"
                await asyncio.sleep(delay)

            resp = await jobs_tools.wait_job(pool, VIEWER_CTX, job_id, timeout_s=0.01, sleep=_sleep)
        # Returned the non-terminal envelope (did not hold open to MAX_WAIT_S = 300s)...
        assert resp.status == "queued"
        # ...and the envelope tells the agent to call jobs.wait again — the "call again" signal.
        assert "jobs.wait" in resp.suggested_next_actions

    asyncio.run(_run())


@pytest.mark.parametrize("timeout_s", [float("nan"), float("inf"), float("-inf")])
def test_wait_non_finite_timeout_is_configuration_error(
    migrated_url: str, timeout_s: float
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            resp = await jobs_tools.wait_job(pool, VIEWER_CTX, job_id, timeout_s=timeout_s)
        assert resp.object_id == job_id
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_wait_loops_until_terminal(migrated_url: str) -> None:
    """Exercise the sleep-then-re-poll branch without a wall-clock delay."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            polls = 0

            async def _cancel_after_first_poll(_: float) -> None:
                nonlocal polls
                polls += 1
                await jobs_tools.cancel_job(pool, OP_CTX, job_id)

            resp = await jobs_tools.wait_job(
                pool,
                VIEWER_CTX,
                job_id,
                timeout_s=5.0,
                sleep=_cancel_after_first_poll,
            )
        assert resp.status == "canceled"
        assert polls == 1

    asyncio.run(_run())


def test_list_jobs_newest_first_and_capped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(3):
                await _enqueue(pool, f"d{i}")
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=2)
        items = resp.items
        assert resp.object_id == "jobs"
        assert resp.status == "ok"
        assert resp.data["count"] == 2
        assert len(items) == 2
        assert all(r.status == "queued" for r in items)

    asyncio.run(_run())


def test_list_jobs_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=50)
        assert resp.status == "ok"
        assert resp.items == []

    asyncio.run(_run())


def test_list_jobs_isolates_invariant_violating_row(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A single producer-bug row (failed with no category) degrades to an error
    envelope without blanking the rest of the list."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            good_id = await _enqueue(pool, "good")
            bad_id = await _enqueue(pool, "bad")
            # Force the bad row into a state that violates "category iff failed".
            await _mark_failed_without_category(pool, bad_id)
            caplog.set_level(logging.WARNING, logger=jobs_tools.__name__)
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=50)
        items = resp.items
        by_id = {r.object_id: r for r in items}
        assert len(items) == 2  # the bad row did not blank the list
        assert by_id[good_id].status == "queued"
        assert by_id[bad_id].status == "error"
        assert by_id[bad_id].error_category == "infrastructure_failure"
        assert any(
            record.exc_info is not None and f"job {bad_id}" in record.message
            for record in caplog.records
        )

    asyncio.run(_run())


# --- cross-project isolation (#11): a job is visible only to its project's members ---

_OTHER = RequestContext(principal="user-2", agent_session="s", projects=("other",))


def test_get_job_in_unowned_project_is_indistinguishable_from_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue_in(pool, "d1", "proj")
            # _OTHER is a member of "other", not "proj": the job must look absent (no leak).
            resp = await jobs_tools.get_job(pool, _OTHER, job_id)
        assert resp.status == "error"
        assert resp.error_category == "not_found"
        assert resp.object_id == job_id

    asyncio.run(_run())


def test_wait_job_in_unowned_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue_in(pool, "d1", "proj")
            resp = await jobs_tools.wait_job(pool, _OTHER, job_id, timeout_s=0.0)
        assert resp.status == "error"
        assert resp.error_category == "not_found"

    asyncio.run(_run())


def test_get_job_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            with pytest.raises(RoleDenied) as excinfo:
                await jobs_tools.get_job(pool, CTX, job_id)
        assert excinfo.value.principal == "user-1"
        assert excinfo.value.project == "proj"
        assert excinfo.value.required is Role.VIEWER

    asyncio.run(_run())


def test_wait_job_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            with pytest.raises(RoleDenied) as excinfo:
                await jobs_tools.wait_job(pool, CTX, job_id, timeout_s=0.0)
        assert excinfo.value.principal == "user-1"
        assert excinfo.value.project == "proj"
        assert excinfo.value.required is Role.VIEWER

    asyncio.run(_run())


def test_cancel_job_in_unowned_project_is_denied_and_does_not_mutate(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue_in(pool, "d1", "proj")
            denied = await jobs_tools.cancel_job(pool, _OTHER, job_id)
            # The owning project's member still sees it queued — the cancel did not land.
            owned = await jobs_tools.get_job(pool, VIEWER_CTX, job_id)
        assert denied.status == "error"
        assert denied.error_category == "not_found"
        assert owned.status == "queued"

    asyncio.run(_run())


def test_cancel_job_requires_contributor_role(migrated_url: str) -> None:
    # Leaseholder-control (#1080, ADR-0320): a viewer cannot cancel — the gate is contributor.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            with pytest.raises(RoleDenied) as excinfo:
                await jobs_tools.cancel_job(pool, VIEWER_CTX, job_id)
            owned = await jobs_tools.get_job(pool, VIEWER_CTX, job_id)
        assert excinfo.value.principal == "user-1"
        assert excinfo.value.project == "proj"
        assert excinfo.value.held is Role.VIEWER
        assert excinfo.value.required is Role.CONTRIBUTOR
        assert owned.status == "queued"

    asyncio.run(_run())


def test_cancel_job_member_overreach_is_audited_at_dispatch_boundary(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            middleware = DenialAuditMiddleware(pool, agent_session=lambda: "s")

            async def _call_next(_ctx: Any) -> object:
                return await jobs_tools.cancel_job(pool, VIEWER_CTX, job_id)

            resp = await middleware.on_call_tool(
                _FakeContext("jobs.cancel", {"job_id": job_id}), _call_next
            )
            owned = await jobs_tools.get_job(pool, VIEWER_CTX, job_id)
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT principal, agent_session, project, tool, transition "
                    "FROM audit_log ORDER BY ts"
                )
                rows = await cur.fetchall()
        assert resp.error_category == "authorization_denied"
        assert owned.status == "queued"
        assert rows == [("user-1", "s", "proj", "jobs.cancel", "denied")]

    asyncio.run(_run())


def test_cancel_job_requires_a_project_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            with pytest.raises(RoleDenied) as excinfo:
                await jobs_tools.cancel_job(pool, CTX, job_id)
            owned = await jobs_tools.get_job(pool, VIEWER_CTX, job_id)
        assert excinfo.value.principal == "user-1"
        assert excinfo.value.project == "proj"
        assert excinfo.value.held is None
        assert excinfo.value.required is Role.CONTRIBUTOR
        assert owned.status == "queued"

    asyncio.run(_run())


def test_list_jobs_only_returns_callers_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            mine = await _enqueue_in(pool, "mine", "proj")
            await _enqueue_in(pool, "theirs", "other")
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=50)
        ids = {r.object_id for r in resp.items}
        assert ids == {mine}  # the "other"-project job is not listed

    asyncio.run(_run())


def test_list_jobs_excludes_roleless_projects(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _enqueue(pool, "d1")
            resp = await jobs_tools.list_jobs(pool, CTX, limit=50)
        assert resp.items == []

    asyncio.run(_run())


def test_list_jobs_excludes_jobs_with_no_project(migrated_url: str) -> None:
    # A job whose authorizing tuple carries no project belongs to no one: fail closed.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
                    "VALUES ('build', %s, 'queued', 3, %s, 'noproj')",
                    (
                        Jsonb(_build_payload().model_dump(mode="json", exclude_none=True)),
                        Jsonb({"principal": "p"}),
                    ),
                )
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=50)
        assert resp.items == []

    asyncio.run(_run())


def test_list_jobs_first_page_sets_truncated_and_cursor(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(3):
                await _enqueue(pool, f"p{i}")
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=2)
        assert resp.data["truncated"] is True
        assert isinstance(resp.data["next_cursor"], str)
        assert len(resp.items) == 2

    asyncio.run(_run())


def test_list_jobs_no_truncation_at_exactly_limit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(2):
                await _enqueue(pool, f"e{i}")
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=2)
        assert resp.data["truncated"] is False
        assert resp.data["next_cursor"] is None

    asyncio.run(_run())


def test_list_jobs_empty_pagination_fields(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=50)
        assert resp.data["truncated"] is False
        assert resp.data["next_cursor"] is None
        assert resp.data["count"] == 0

    asyncio.run(_run())


def test_list_jobs_drains_every_row_following_cursor(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(5):
                await _enqueue(pool, f"d{i}")
            seen: list[str] = []
            cursor: str | None = None
            for _ in range(10):  # bound the loop defensively
                resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=2, cursor=cursor)
                seen.extend(item.object_id for item in resp.items)
                if not resp.data["truncated"]:
                    break
                next_cursor = resp.data["next_cursor"]
                assert isinstance(next_cursor, str)
                cursor = next_cursor
        assert len(seen) == 5
        assert len(set(seen)) == 5  # no duplicate across pages

    asyncio.run(_run())


def test_list_jobs_malformed_cursor_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=2, cursor="!!!")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_cursor"

    asyncio.run(_run())


async def _set_state(pool: AsyncConnectionPool, job_id: str, state: JobState) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE jobs SET state = %s WHERE id = %s", (state.value, job_id))


async def _enqueue_provision(pool: AsyncConnectionPool, dedup: str) -> str:
    async with pool.connection() as conn:
        job = await queue.enqueue(
            conn,
            JobKind.PROVISION,
            SystemPayload(system_id=str(uuid4())),
            Authorizing(principal="p", project="proj"),
            dedup,
        )
    return str(job.id)


async def _enqueue_build_for_investigation(pool: AsyncConnectionPool, dedup: str) -> str:
    """Seed an Investigation + Run and a build job whose payload run_id points at it.

    Returns the Investigation id (the filter key). ``runs.system_id`` is nullable since
    ADR-0169, so the Run needs only its Investigation FK + a committed ``target_kind``.
    """
    async with pool.connection() as conn:
        inv = await conn.execute(
            "INSERT INTO investigations (title, state, principal, project) "
            "VALUES ('inv', 'active', 'p', 'proj') RETURNING id"
        )
        inv_row = await inv.fetchone()
        assert inv_row is not None
        run = await conn.execute(
            "INSERT INTO runs (investigation_id, state, build_profile, principal, project, "
            "target_kind) VALUES (%s, 'running', '{}'::jsonb, 'p', 'proj', 'local_libvirt') "
            "RETURNING id",
            (inv_row[0],),
        )
        run_row = await run.fetchone()
        assert run_row is not None
        await queue.enqueue(
            conn,
            JobKind.BUILD,
            BuildPayload(run_id=str(run_row[0]), build_host_id=str(WORKER_LOCAL_ID)),
            Authorizing(principal="p", project="proj"),
            dedup,
        )
    return str(inv_row[0])


def test_list_jobs_filters_by_status(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            failed = await _enqueue(pool, "f")
            await _set_state(pool, failed, JobState.FAILED)
            await _enqueue(pool, "q")  # stays queued
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=50, status=JobState.FAILED)
        assert [r.object_id for r in resp.items] == [failed]

    asyncio.run(_run())


def test_list_jobs_filters_by_kind(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            build_id = await _enqueue(pool, "b")
            await _enqueue_provision(pool, "p")
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=50, kind=JobKind.BUILD)
        assert [r.object_id for r in resp.items] == [build_id]

    asyncio.run(_run())


def test_list_jobs_filters_by_investigation_id(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            investigation_id = await _enqueue_build_for_investigation(pool, "in-inv")
            await _enqueue(pool, "other")  # build for an unrelated run
            await _enqueue_provision(pool, "no-run")  # run-less, never matches
            resp = await jobs_tools.list_jobs(
                pool, VIEWER_CTX, limit=50, investigation_id=investigation_id
            )
        assert [r.data["kind"] for r in resp.items] == ["build"]
        assert len(resp.items) == 1

    asyncio.run(_run())


def test_list_jobs_malformed_investigation_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await jobs_tools.list_jobs(
                pool, VIEWER_CTX, limit=50, investigation_id="not-a-uuid"
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_uuid"

    asyncio.run(_run())


def test_list_jobs_investigation_filter_no_match_is_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _enqueue(pool, "b")
            resp = await jobs_tools.list_jobs(
                pool, VIEWER_CTX, limit=50, investigation_id=str(uuid4())
            )
        assert resp.status == "ok"
        assert resp.items == []

    asyncio.run(_run())
