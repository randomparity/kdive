"""The live-tier harness login holds exactly the harness grants (#3196, ADR-0769).

Gated ``live_stack``: it needs the local compose database after the role bootstrap has run
(``scripts/live-stack/stack-services.sh``, or the ``role-bootstrap`` one-shot after
``just stack-backends``). It connects as ``KDIVE_HARNESS_DATABASE_URL`` and observes the grants
directly, so a missing role, a missing grant, or a widened 0104 revocation fails here rather than
in the middle of a cell.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import psycopg
import pytest

from kdive.db.repositories import ALLOCATIONS
from kdive.domain.capacity.state import AllocationState
from kdive.domain.lifecycle.records import Allocation
from kdive.providers.core.resource_registration import register_discovered_resource
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from tests.integration._seed import _DT
from tests.integration.live_stack.spine import seed_metering
from tests.integration.live_stack.tool_cells import require_snapshot_read_access
from tests.live_vm.installed_local_authority_support import require_evidence_read_access
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

pytestmark = pytest.mark.live_stack

_HARNESS_DSN_ENV = "KDIVE_HARNESS_DATABASE_URL"
_WRITES = {
    ("allocations", "INSERT"),
    ("budgets", "INSERT"),
    ("budgets", "UPDATE"),
    ("quotas", "INSERT"),
    ("quotas", "UPDATE"),
    ("resources", "INSERT"),
    ("resources", "UPDATE"),
}
_FENCE_TABLES = (
    "worker_incarnations",
    "investigation_build_uses",
    "investigation_build_use_recoveries",
)
_RUNTIME_ROLES = ("kdive_server", "kdive_worker", "kdive_reconciler", "kdive_lifecycle_witness")
_DDL = (
    "CREATE TABLE public.harness_probe (x int)",
    "ALTER TABLE public.budgets ADD COLUMN harness_probe int",
    "DROP TABLE public.quotas",
)


async def _rows(
    conn: psycopg.AsyncConnection, query: str, params: tuple | None = None
) -> list[tuple]:
    cursor = await conn.execute(query, params)  # ty: ignore[invalid-argument-type]
    return await cursor.fetchall()


async def _assert_table_privileges(conn: psycopg.AsyncConnection) -> None:
    unreadable = await _rows(
        conn,
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "AND NOT has_table_privilege(format('public.%I', tablename), 'SELECT')",
    )
    assert unreadable == [], f"harness cannot SELECT {unreadable}"
    writes = await _rows(
        conn,
        "SELECT t.tablename, p.privilege FROM pg_tables AS t "
        "CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) AS p(privilege) "
        "WHERE t.schemaname = 'public' "
        "AND has_table_privilege(format('public.%I', t.tablename), p.privilege)",
    )
    assert set(writes) == _WRITES


async def _assert_no_ddl_membership_or_ownership(conn: psycopg.AsyncConnection) -> None:
    [(may_create,)] = await _rows(conn, "SELECT has_schema_privilege('public', 'CREATE')")
    assert may_create is False
    for statement in _DDL:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with conn.transaction(force_rollback=True):
                await conn.execute(statement)
    memberships = await _rows(
        conn,
        "SELECT roleid::regrole::text FROM pg_auth_members WHERE member = current_user::regrole",
    )
    assert memberships == []
    owned = await _rows(
        conn,
        "SELECT relname FROM pg_class WHERE relowner = current_user::regrole "
        "UNION ALL SELECT nspname FROM pg_namespace WHERE nspowner = current_user::regrole",
    )
    assert owned == []


async def _assert_fence_revocations_hold(conn: psycopg.AsyncConnection) -> None:
    widened = await _rows(
        conn,
        "SELECT r.role, t.name FROM unnest(%s::text[]) AS r(role) "
        "CROSS JOIN unnest(%s::text[]) AS t(name) "
        "WHERE has_table_privilege(r.role, format('public.%%I', t.name), 'SELECT')",
        (list(_RUNTIME_ROLES), list(_FENCE_TABLES)),
    )
    assert widened == [], f"0104 revocation widened: {widened}"
    public = await _rows(
        conn,
        "SELECT c.relname FROM pg_class AS c CROSS JOIN LATERAL aclexplode(c.relacl) AS acl "
        "WHERE c.oid = ANY(%s::regclass[]) AND acl.grantee = 0",
        ([f"public.{name}" for name in _FENCE_TABLES],),
    )
    assert public == [], f"0104 table granted to PUBLIC: {public}"


async def _assert_harness_reads_and_seeds(conn: psycopg.AsyncConnection, dsn: str) -> None:
    tables = await _rows(
        conn,
        "SELECT c.relname FROM pg_attribute AS a JOIN pg_class AS c ON c.oid = a.attrelid "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
        "AND c.relkind IN ('r', 'p') AND a.attname = 'project' AND NOT a.attisdropped",
    )
    await require_snapshot_read_access(conn, [str(row[0]) for row in tables])
    await require_evidence_read_access(dsn)
    await seed_metering(dsn, "harness-role-probe")
    discovery = LocalLibvirtDiscovery(
        host_uri="qemu:///harness-role-probe",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=1,
    )
    async with conn.transaction(force_rollback=True):
        resource = await register_discovered_resource(
            conn, discovery.list_resources()[0], pool="local-libvirt", cost_class="local"
        )
        await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="harness-role-probe",
                project="harness-role-probe",
                resource_id=resource.id,
                state=AllocationState.GRANTED,
            ),
        )


def test_harness_login_holds_exactly_the_harness_grants() -> None:
    dsn = os.environ.get(_HARNESS_DSN_ENV)
    if not dsn:
        pytest.skip(f"{_HARNESS_DSN_ENV} unset; source scripts/live-stack/env.sh")

    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            await _assert_table_privileges(conn)
            await _assert_no_ddl_membership_or_ownership(conn)
            await _assert_fence_revocations_hold(conn)
            await _assert_harness_reads_and_seeds(conn, dsn)

    asyncio.run(run())
