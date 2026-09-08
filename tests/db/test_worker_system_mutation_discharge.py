"""Worker/reconciler-fenced bulk mutation-obligation discharge (ADR-0629, migration 0152).

The System-teardown reclaim path issues the bulk ``terminal_escape`` discharge under
``kdive_worker`` or ``kdive_reconciler``, and migration 0126 grants both roles ``SELECT`` only on
``remote_module_attempt_obligations``. Before #2302 that made ``systems.teardown`` fail with
``permission denied for table``. These arms hold the fix in place from both directions:

- **Never run an arm as the superuser.** The suite connects as the backend superuser by default,
  and ``pg_has_role`` is true for a superuser against every role — so a superuser arm would assert
  nothing about the in-body gate. Every arm here goes through a real ``LOGIN`` principal.
- **A permitted arm and a denied arm for the same call.** A function broken for everyone would
  deny a non-member too, so the grant is proven by the worker and reconciler arms succeeding while
  the non-member arm is refused.
- **The grant is a function grant, not a table grant.** That difference is observable only in
  ``test_worker_role_direct_update_still_denied``; without it a table-level ``UPDATE`` would
  pass every other arm here.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.errors import InsufficientPrivilege
from psycopg.sql import SQL, Identifier, Literal

from kdive.db.remote_module_attempt_obligations import RemoteModuleAttemptObligationRepository
from kdive.jobs.handlers.system_reclaim import reclaim_system_core_after_provider_teardown
from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.external_boot_authority_support import (
    authority_role_dsns as authority_role_dsns,  # noqa: F401
)
from tests.db.remote_module_attempt_obligations_support import _attempt, _seed

_NON_MEMBER_PASSWORD = "worker-discharge-non-member"  # pragma: allowlist secret
_DIRECT_UPDATE = (
    "UPDATE remote_module_attempt_obligations "
    "SET mutation_discharged_at = now(), mutation_discharge_reason = 'terminal_escape' "
    "WHERE system_id = %s AND mutation_discharged_at IS NULL"
)
_READ_DISCHARGE = (
    "SELECT mutation_discharged_at, mutation_discharge_reason "
    "FROM remote_module_attempt_obligations WHERE system_id = %s"
)


class _NoStore:
    """The reclaim helper's object-store port; these arms assert on database state only."""

    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        return True


async def _seed_open_obligation(migrated_url: str) -> UUID:
    """Insert one System carrying one open mutation obligation, and commit it."""
    async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
        system_id, run_id = await _seed(admin)
        assert await RemoteModuleAttemptObligationRepository().open_mutation_obligation(
            admin, _attempt(system_id, run_id)
        )
        await admin.commit()
    return system_id


async def _reclaim_as(dsn: str, system_id: UUID, *, reclaim_snapshot_ledger: bool) -> None:
    """Run the teardown reclaim path over one role's connection, as the handler calls it."""
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await reclaim_system_core_after_provider_teardown(
            conn,
            _NoStore(),
            system_id,
            reclaim_snapshot_ledger=reclaim_snapshot_ledger,
            discharge_mutation_obligations=True,
        )
        await conn.commit()


async def _read_discharge(migrated_url: str, system_id: UUID) -> tuple[object, str | None]:
    async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
        row = await (await admin.execute(_READ_DISCHARGE, (system_id,))).fetchone()
    assert row is not None
    return row[0], row[1]


# The three shapes production actually passes. `reclaim_snapshot_ledger=True` additionally
# runs `delete_snapshots_for_system`, so covering it under both roles keeps a later revocation
# on that branch from reddening one role's arm while leaving the other's unproven.
_RECLAIM_SHAPES = [
    pytest.param("kdive_worker", False, id="worker-systems-teardown"),
    pytest.param("kdive_worker", True, id="worker-authority-teardown"),
    pytest.param("kdive_reconciler", True, id="reconciler-repair"),
]


@pytest.mark.parametrize(("role", "reclaim_snapshot_ledger"), _RECLAIM_SHAPES)
def test_role_teardown_reclaim_discharges(
    migrated_url: str, authority_role_dsns: _RoleDsns, role: str, reclaim_snapshot_ledger: bool
) -> None:
    """The reported failure, at each call site's role and flag combination.

    `jobs/handlers/systems.py:730` passes (`kdive_worker`, False),
    `jobs/handlers/system_authority.py:223` passes (`kdive_worker`, True), and
    `reconciler/repairs/jobs.py:98` passes (`kdive_reconciler`, True).
    """

    async def run() -> None:
        system_id = await _seed_open_obligation(migrated_url)
        await _reclaim_as(
            authority_role_dsns(role),
            system_id,
            reclaim_snapshot_ledger=reclaim_snapshot_ledger,
        )
        discharged_at, reason = await _read_discharge(migrated_url, system_id)
        assert discharged_at is not None
        assert reason == "terminal_escape"

    asyncio.run(run())


def test_worker_role_direct_update_still_denied(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    """The fix is EXECUTE on one function, never table-level write (ADR-0629 exclusion 1)."""

    async def run() -> None:
        system_id = await _seed_open_obligation(migrated_url)
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_worker")
        ) as worker:
            with pytest.raises(InsufficientPrivilege):
                await worker.execute(_DIRECT_UPDATE, (system_id,))
            await worker.rollback()

    asyncio.run(run())


def test_non_member_execute_is_refused(migrated_url: str, authority_role_dsns: _RoleDsns) -> None:
    """The in-body gate, proven apart from the EXECUTE grant.

    Reaching the gate takes a principal that *can* execute the function and is a member of
    neither role, which the migration's grant means no production role is. The arm constructs
    exactly that principal rather than claiming a production role produces this message.
    """

    async def run() -> None:
        system_id = await _seed_open_obligation(migrated_url)
        login = f"kdive_wsmd_nonmember_{uuid4().hex[:16]}"
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as admin:
            await admin.execute(
                SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    Identifier(login), Literal(_NON_MEMBER_PASSWORD)
                )
            )
            try:
                await admin.execute(
                    SQL(
                        "GRANT EXECUTE ON FUNCTION "
                        "public.discharge_system_mutation_obligations(uuid) TO {}"
                    ).format(Identifier(login))
                )
                dsn = make_conninfo(
                    **{
                        **authority_role_dsns.parameters,
                        "user": login,
                        "password": _NON_MEMBER_PASSWORD,
                    }
                )
                async with await psycopg.AsyncConnection.connect(dsn) as outsider:
                    with pytest.raises(InsufficientPrivilege) as refused:
                        await outsider.execute(
                            "SELECT public.discharge_system_mutation_obligations(%s)",
                            (system_id,),
                        )
                    assert "worker or reconciler authority is required" in str(refused.value)
                    await outsider.rollback()
            finally:
                # The EXECUTE grant is a dependent object, so DROP ROLE alone fails with
                # `cannot be dropped because some objects depend on it`.
                await admin.execute(SQL("DROP OWNED BY {}").format(Identifier(login)))
                await admin.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(login)))

    asyncio.run(run())


def test_second_discharge_is_a_noop(migrated_url: str, authority_role_dsns: _RoleDsns) -> None:
    """First-write-wins: the `mutation_discharged_at IS NULL` predicate holds the first evidence."""

    async def run() -> None:
        system_id = await _seed_open_obligation(migrated_url)
        dsn = authority_role_dsns("kdive_worker")
        await _reclaim_as(dsn, system_id, reclaim_snapshot_ledger=False)
        first = await _read_discharge(migrated_url, system_id)
        await _reclaim_as(dsn, system_id, reclaim_snapshot_ledger=False)
        assert await _read_discharge(migrated_url, system_id) == first

    asyncio.run(run())


def test_server_role_shared_discharge_still_works(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    """The shared method keeps its direct write for the two `kdive_server` activation edges.

    `db/external_boot_activations.py:552` and `:886` call it on the `abandoned` and
    `recovery_failed` edges, and `0104_worker_fence_roles.sql` establishes that `kdive_server` is
    a member of no role — so rerouting the shared method would deny it (ADR-0629).
    """

    async def run() -> None:
        system_id = await _seed_open_obligation(migrated_url)
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_server")
        ) as server:
            repo = RemoteModuleAttemptObligationRepository()
            async with server.transaction():
                discharged = await repo.discharge_system_mutation_obligations(server, system_id)
            assert discharged == 1
        assert (await _read_discharge(migrated_url, system_id))[1] == "terminal_escape"

    asyncio.run(run())
