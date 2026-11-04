"""BYO representation prerequisites, with no operational writers (ADR-0813)."""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db import migrate


def _resource(conn: psycopg.Connection) -> UUID:
    row = conn.execute(
        "INSERT INTO resources(kind, name, pool, cost_class, status, host_uri) "
        "VALUES ('byo-host', %s, 'default', 'byo', 'available', 'target.example') RETURNING id",
        (f"target-{uuid4()}",),
    ).fetchone()
    assert row is not None
    return row[0]


def test_byo_coefficient_is_seeded_and_runner_is_idempotent(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT coeff FROM cost_class_coefficients WHERE cost_class='byo'"
    ).fetchone()
    assert row == (Decimal("1.0"),)
    assert migrate.apply_migrations(pg_conn) == []


def test_byo_migration_preserves_operator_coefficient(
    pg_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = migrate.SCHEMA_DIR
    for path in source.glob("*.sql"):
        if path.name < "0190_":
            shutil.copyfile(path, tmp_path / path.name)
    monkeypatch.setattr(migrate, "SCHEMA_DIR", tmp_path)
    migrate.apply_migrations(pg_conn)
    pg_conn.execute("INSERT INTO cost_class_coefficients VALUES ('byo', 2.75)")
    migration = source / "0190_byo_host_resources.sql"
    shutil.copyfile(migration, tmp_path / migration.name)
    assert migrate.apply_migrations(pg_conn) == ["0190"]
    assert pg_conn.execute(
        "SELECT coeff FROM cost_class_coefficients WHERE cost_class='byo'"
    ).fetchone() == (Decimal("2.75"),)


def _lease(
    conn: psycopg.Connection, resource: UUID, mode: str, *, worker: str = "worker-a"
) -> None:
    conn.execute(
        "INSERT INTO byo_console_leases(id, resource_id, mode, holder_worker_id, expires_at) "
        "VALUES (%s, %s, %s, %s, now()+interval '1 minute')",
        (uuid4(), resource, mode, worker),
    )


def test_console_lease_resource_identity_modes_and_expiry(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    readers, preemptor = _resource(pg_conn), _resource(pg_conn)
    _lease(pg_conn, readers, "reading")
    _lease(pg_conn, readers, "reading", worker="worker-b")
    _lease(pg_conn, preemptor, "preempting")
    with pytest.raises(psycopg.errors.UniqueViolation), pg_conn.transaction():
        _lease(pg_conn, preemptor, "preempting", worker="worker-b")
    for mode, worker in (("collector", "worker-a"), ("reading", ""), ("reading", "x" * 513)):
        with pytest.raises(psycopg.errors.CheckViolation), pg_conn.transaction():
            _lease(pg_conn, readers, mode, worker=worker)
    with pytest.raises(psycopg.errors.CheckViolation), pg_conn.transaction():
        pg_conn.execute(
            "INSERT INTO byo_console_leases(id,resource_id,mode,holder_worker_id,expires_at) "
            "VALUES (%s,%s,'reading','worker-a',now())",
            (uuid4(), readers),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation), pg_conn.transaction():
        _lease(pg_conn, uuid4(), "reading")
    pg_conn.execute("DELETE FROM resources WHERE id=%s", (preemptor,))
    assert pg_conn.execute(
        "SELECT count(*) FROM byo_console_leases WHERE resource_id=%s", (preemptor,)
    ).fetchone() == (0,)
    assert pg_conn.execute("SELECT count(*) FROM byo_console_leases").fetchone() == (2,)


def test_adopt_facts_are_nullable_and_object_shaped(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    resource = _resource(pg_conn)
    allocation = pg_conn.execute(
        "INSERT INTO allocations(resource_id,state,principal,project) "
        "VALUES (%s,'granted','actor','project') RETURNING id",
        (resource,),
    ).fetchone()
    assert allocation is not None
    system = pg_conn.execute(
        "INSERT INTO systems(allocation_id,state,provisioning_profile,principal,project) "
        "VALUES (%s,'provisioning','{}','actor','project') RETURNING id,byo_adopt_facts",
        (allocation[0],),
    ).fetchone()
    assert system is not None and system[1] is None
    pg_conn.execute("UPDATE systems SET byo_adopt_facts='{}' WHERE id=%s", (system[0],))
    for invalid in ("[]", "null", "1", '"not-an-object"'):
        with pytest.raises(psycopg.errors.CheckViolation), pg_conn.transaction():
            pg_conn.execute(
                "UPDATE systems SET byo_adopt_facts=%s::jsonb WHERE id=%s", (invalid, system[0])
            )


def test_unused_console_table_grants_no_runtime_authority(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    for role in ("kdive_server", "kdive_worker", "kdive_reconciler", "kdive_lifecycle_witness"):
        assert pg_conn.execute(
            "SELECT has_table_privilege(%s,'byo_console_leases','INSERT,UPDATE,DELETE')", (role,)
        ).fetchone() == (False,)


def test_system_repository_adapts_and_reads_nullable_adopt_facts(
    pg_conn: psycopg.Connection,
    postgres_url: str,
) -> None:
    import asyncio

    from kdive.db.repositories import SYSTEMS

    migrate.apply_migrations(pg_conn)
    resource = _resource(pg_conn)

    def allocation() -> UUID:
        row = pg_conn.execute(
            "INSERT INTO allocations(resource_id,state,principal,project) "
            "VALUES (%s,'granted','actor','project') RETURNING id",
            (resource,),
        ).fetchone()
        assert row is not None
        return row[0]

    row = pg_conn.execute(
        "INSERT INTO systems(allocation_id,state,provisioning_profile,principal,project) "
        "VALUES (%s,'provisioning','{}','actor','project') RETURNING id",
        (allocation(),),
    ).fetchone()
    assert row is not None

    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(postgres_url, autocommit=True) as conn:
            original = await SYSTEMS.get(conn, row[0])
            assert original is not None and original.byo_adopt_facts is None
            copied = original.model_copy(
                update={
                    "id": uuid4(),
                    "allocation_id": allocation(),
                    "byo_adopt_facts": {"fixture": "reader-only-test", "verified": False},
                }
            )
            inserted = await SYSTEMS.insert(conn, copied)
            restored = await SYSTEMS.get(conn, inserted.id)
            assert restored is not None and restored.byo_adopt_facts == copied.byo_adopt_facts

    asyncio.run(run())
