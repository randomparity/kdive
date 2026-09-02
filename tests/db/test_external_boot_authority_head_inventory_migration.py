"""Migration 0125 bounded authority inventory and peer authentication proofs."""

from __future__ import annotations

import asyncio
import hashlib
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.sql import SQL, Identifier
from pydantic import SecretStr

from kdive.db import external_boot_authority_journal as journal_repository
from kdive.db import migrate
from kdive.providers.external_boot_authority.host import HostReadinessError, check_database_role
from kdive.providers.external_boot_authority.protocol import JournalPhase
from kdive.providers.external_boot_authority.service import AuthenticatedPeer
from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.external_boot_authority_support import (
    authority_role_dsns as authority_role_dsns,  # noqa: F401
)

_DIGEST = "sha256:" + "d" * 64
_FUNCTIONS = {
    "list_external_boot_authority_journal_heads(text)",
    "authenticate_external_boot_authority_peer(bytea)",
}
_RUNTIME_ROLES = {
    "PUBLIC",
    "kdive_server",
    "kdive_worker",
    "kdive_reconciler",
    "kdive_lifecycle_witness",
    "kdive_provider_authority",
}


def _seed_heads(
    conn: psycopg.Connection,
    *,
    authority_instance: str,
    count: int,
) -> tuple[UUID, ...]:
    resource_id, allocation_id, authority_id = uuid4(), uuid4(), uuid4()
    conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'default', 'standard', 'available', 'qemu:///system')",
        (resource_id,),
    )
    conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'active', 'p', 'proj')",
        (allocation_id, resource_id),
    )
    rows = conn.execute(
        "INSERT INTO systems "
        "(id, allocation_id, state, provisioning_profile, principal, project) "
        "SELECT gen_random_uuid(), %s, 'ready', '{}'::jsonb, 'p', 'proj' "
        "FROM generate_series(1, %s) RETURNING id",
        (allocation_id, count),
    ).fetchall()
    system_ids = tuple(sorted((row[0] for row in rows), key=str))
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO external_boot_authority_journal_heads "
            "(authority_instance, system_id, sequence, digest, phase, authority_id, generation, "
            "operation_identity, head_record) VALUES (%s, %s, %s, %s, 'terminal', %s, 1, %s, "
            "'{}'::jsonb)",
            (
                (
                    authority_instance,
                    system_id,
                    sequence,
                    _DIGEST,
                    authority_id,
                    f"operation-{sequence}",
                )
                for sequence, system_id in enumerate(system_ids, start=1)
            ),
        )
    return system_ids


def _insert_worker(
    conn: psycopg.Connection,
    *,
    incarnation: str,
    credential_hash: bytes,
    fence_protocol: int = 4,
    active: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO worker_incarnations "
        "(incarnation, authority_kind, authority_binding, credential_hash, fence_protocol, "
        "state, terminated_at, outcome) VALUES (%s, 'docker', '{}'::jsonb, %s, %s, %s, "
        "CASE WHEN %s THEN NULL ELSE now() END, CASE WHEN %s THEN NULL ELSE 'killed' END)",
        (
            incarnation,
            credential_hash,
            fence_protocol,
            "active" if active else "terminated",
            active,
            active,
        ),
    )


def test_migration_0125_is_the_unique_bounded_inventory_tail() -> None:
    migrations = migrate.discover_migrations()
    assert (migrations[-1].version, migrations[-1].filename) == (
        "0125",
        "0125_external_boot_authority_head_inventory.sql",
    )
    migration = migrations[-1]
    assert migration.sql.count("LIMIT 4097") == 1
    assert "list_external_boot_authority_journal_heads" in migration.sql
    assert "p_authority_instance text" in migration.sql


def test_functions_are_authority_only_without_new_table_or_lifecycle_access(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as admin:
        privileges: dict[tuple[str, str], bool] = {}
        for role in _RUNTIME_ROLES - {"PUBLIC"}:
            for signature in _FUNCTIONS:
                row = admin.execute(
                    "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, signature)
                ).fetchone()
                assert row is not None
                privileges[(role, signature)] = bool(row[0])
        public_privileges: dict[str, bool] = {}
        for signature in _FUNCTIONS:
            row = admin.execute(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_proc AS function_row "
                "CROSS JOIN LATERAL aclexplode(coalesce(function_row.proacl, "
                "acldefault('f', function_row.proowner))) AS privilege "
                "WHERE function_row.oid=%s::regprocedure AND privilege.grantee=0 "
                "AND privilege.privilege_type='EXECUTE')",
                (signature,),
            ).fetchone()
            assert row is not None
            public_privileges[signature] = bool(row[0])
        assert {role for (role, _signature), allowed in privileges.items() if allowed} == {
            "kdive_provider_authority"
        }
        assert not any(public_privileges.values())
        assert set(
            admin.execute(
                "SELECT table_name, privilege_type "
                "FROM information_schema.role_table_grants "
                "WHERE grantee='kdive_provider_authority' ORDER BY 1, 2"
            ).fetchall()
        ) == {
            ("external_boot_authorities", "SELECT"),
            ("external_boot_authority_acknowledgements", "SELECT"),
        }

    for role in set(authority_role_dsns.logins) - {"kdive_provider_authority"}:
        with psycopg.connect(authority_role_dsns(role), autocommit=True) as denied:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                denied.execute("SELECT * FROM list_external_boot_authority_journal_heads('a')")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                denied.execute(
                    "SELECT * FROM authenticate_external_boot_authority_peer(%s)",
                    (b"x" * 32,),
                )

    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as authority:
        assert (
            authority.execute(
                "SELECT * FROM list_external_boot_authority_journal_heads('missing')"
            ).fetchall()
            == []
        )
        assert (
            authority.execute(
                "SELECT * FROM authenticate_external_boot_authority_peer(%s)", (b"x" * 32,)
            ).fetchone()
            is None
        )
        for statement in (
            "SELECT * FROM external_boot_authority_journal_heads",
            "SELECT * FROM worker_incarnations",
            "UPDATE systems SET state=state WHERE false",
            "UPDATE worker_incarnations SET state=state WHERE false",
            "UPDATE external_boot_authority_journal_heads SET sequence=sequence WHERE false",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                authority.execute(statement)


def test_inventory_is_empty_and_instance_filtered(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as admin:
        first_ids = _seed_heads(admin, authority_instance="authority-a", count=2)
        _seed_heads(admin, authority_instance="authority-b", count=1)

    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as authority:
        assert (
            authority.execute(
                "SELECT * FROM list_external_boot_authority_journal_heads('missing')"
            ).fetchall()
            == []
        )
        cursor = authority.execute(
            "SELECT * FROM list_external_boot_authority_journal_heads('authority-a')"
        )
        description = cursor.description
        assert description is not None
        assert [column.name for column in description] == [
            "authority_instance",
            "system_id",
            "sequence",
            "digest",
            "phase",
            "authority_id",
            "generation",
            "operation_identity",
        ]
        rows = cursor.fetchall()
    assert tuple(row[1] for row in rows) == first_ids
    assert {row[0] for row in rows} == {"authority-a"}


@pytest.mark.parametrize(
    ("lane_count", "expected_count"), [(4096, 4096), (4097, 4097), (4098, 4097)]
)
def test_inventory_hard_limit_boundary(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    lane_count: int,
    expected_count: int,
) -> None:
    authority_instance = f"authority-{lane_count}"
    with psycopg.connect(migrated_url) as admin:
        expected_ids = _seed_heads(admin, authority_instance=authority_instance, count=lane_count)

    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as authority:
        rows = authority.execute(
            "SELECT system_id FROM list_external_boot_authority_journal_heads(%s)",
            (authority_instance,),
        ).fetchall()

    assert len(rows) == expected_count
    assert tuple(row[0] for row in rows) == expected_ids[:expected_count]


@pytest.mark.parametrize("credential_hash", [None, b"x" * 31, b"x" * 33])
def test_peer_authentication_rejects_invalid_hash_size(
    authority_role_dsns: _RoleDsns, credential_hash: bytes | None
) -> None:
    with (
        psycopg.connect(
            authority_role_dsns("kdive_provider_authority"), autocommit=True
        ) as authority,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        authority.execute(
            "SELECT * FROM authenticate_external_boot_authority_peer(%s::bytea)",
            (credential_hash,),
        )


def test_peer_authentication_requires_active_fence_protocol_four(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    valid_hash = hashlib.sha256(b"valid").digest()
    inactive_hash = hashlib.sha256(b"inactive").digest()
    wrong_protocol_hash = hashlib.sha256(b"wrong-protocol").digest()
    with psycopg.connect(migrated_url) as admin:
        _insert_worker(admin, incarnation="docker:valid", credential_hash=valid_hash)
        _insert_worker(
            admin,
            incarnation="docker:inactive",
            credential_hash=inactive_hash,
            active=False,
        )
        _insert_worker(
            admin,
            incarnation="docker:wrong-protocol",
            credential_hash=wrong_protocol_hash,
            fence_protocol=5,
        )

    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as authority:
        invalid_hashes = (
            inactive_hash,
            wrong_protocol_hash,
            hashlib.sha256(b"absent").digest(),
        )
        for credential_hash in invalid_hashes:
            assert (
                authority.execute(
                    "SELECT * FROM authenticate_external_boot_authority_peer(%s)",
                    (credential_hash,),
                ).fetchone()
                is None
            )
        cursor = authority.execute(
            "SELECT * FROM authenticate_external_boot_authority_peer(%s)", (valid_hash,)
        )
        description = cursor.description
        assert description is not None
        assert [column.name for column in description] == ["peer_incarnation_id"]
        assert cursor.fetchone() == ("docker:valid",)


def test_typed_readers_return_bounded_heads_and_hash_secret_before_sql(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    secret = "authority-peer-secret"  # pragma: allowlist secret
    credential_hash = hashlib.sha256(secret.encode("utf-8")).digest()
    with psycopg.connect(migrated_url) as admin:
        system_ids = _seed_heads(admin, authority_instance="authority-reader", count=2)
        _insert_worker(
            admin,
            incarnation="docker:typed-reader",
            credential_hash=credential_hash,
        )

    async def read() -> tuple[tuple[journal_repository.JournalHead, ...], AuthenticatedPeer]:
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_provider_authority"), autocommit=True
        ) as authority:
            heads = await journal_repository.list_journal_heads(
                authority, authority_instance="authority-reader"
            )
            peer = await journal_repository.authenticate_authority_peer(
                authority, SecretStr(secret)
            )
            with pytest.raises(journal_repository.AuthorityPeerAuthenticationError):
                await journal_repository.authenticate_authority_peer(
                    authority,
                    SecretStr("invalid-authority-peer-secret"),  # pragma: allowlist secret
                )
        return heads, peer

    heads, peer = asyncio.run(read())
    assert tuple(head.system_id for head in heads) == system_ids
    assert all(
        head.authority_instance == "authority-reader"
        and head.phase is JournalPhase.TERMINAL
        and head.pending_takeover is None
        and head.suspended_operation is None
        for head in heads
    )
    assert peer == AuthenticatedPeer(incarnation_id="docker:typed-reader")


def test_host_role_shape_rejects_effective_grants_and_application_object_ownership(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    login = authority_role_dsns.logins["kdive_provider_authority"]
    capability_role = "kdive_provider_authority"
    owned_table = f"authority_role_shape_{uuid4().hex}"
    granted_schema = f"authority_role_shape_{uuid4().hex}"

    async def validate() -> None:
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_provider_authority"), autocommit=True
        ) as connection:
            await check_database_role(connection)

    asyncio.run(validate())
    with psycopg.connect(migrated_url, autocommit=True) as admin:
        admin.execute(SQL("GRANT SELECT ON public.systems TO {}").format(Identifier(login)))
    try:
        with pytest.raises(HostReadinessError, match="database-role: excessive-privilege"):
            asyncio.run(validate())
    finally:
        with psycopg.connect(migrated_url, autocommit=True) as admin:
            admin.execute(SQL("REVOKE SELECT ON public.systems FROM {}").format(Identifier(login)))

    with psycopg.connect(migrated_url, autocommit=True) as admin:
        admin.execute(
            SQL("GRANT SELECT ON public.systems TO {}").format(Identifier(capability_role))
        )
    try:
        with pytest.raises(HostReadinessError, match="database-role: excessive-privilege"):
            asyncio.run(validate())
    finally:
        with psycopg.connect(migrated_url, autocommit=True) as admin:
            admin.execute(
                SQL("REVOKE SELECT ON public.systems FROM {}").format(Identifier(capability_role))
            )

    with psycopg.connect(migrated_url, autocommit=True) as admin:
        admin.execute(SQL("CREATE SCHEMA {}").format(Identifier(granted_schema)))
        admin.execute(
            SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                Identifier(granted_schema), Identifier(login)
            )
        )
    try:
        with pytest.raises(HostReadinessError, match="database-role: excessive-privilege"):
            asyncio.run(validate())
    finally:
        with psycopg.connect(migrated_url, autocommit=True) as admin:
            admin.execute(SQL("DROP SCHEMA {}").format(Identifier(granted_schema)))

    with psycopg.connect(migrated_url, autocommit=True) as admin:
        admin.execute(SQL("CREATE TABLE public.{} (value integer)").format(Identifier(owned_table)))
        admin.execute(
            SQL("ALTER TABLE public.{} OWNER TO {}").format(
                Identifier(owned_table), Identifier(login)
            )
        )
    try:
        with pytest.raises(HostReadinessError, match="database-role: excessive-privilege"):
            asyncio.run(validate())
    finally:
        with psycopg.connect(migrated_url, autocommit=True) as admin:
            admin.execute(SQL("DROP TABLE public.{}").format(Identifier(owned_table)))


def test_host_role_shape_rejects_public_application_function_privilege(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    public_function = f"authority_role_shape_{uuid4().hex}"

    async def validate() -> None:
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_provider_authority"), autocommit=True
        ) as connection:
            await check_database_role(connection)

    with psycopg.connect(migrated_url, autocommit=True) as admin:
        admin.execute(
            SQL("CREATE FUNCTION public.{}() RETURNS integer LANGUAGE sql AS 'SELECT 1'").format(
                Identifier(public_function)
            )
        )
    try:
        with pytest.raises(HostReadinessError, match="database-role: excessive-privilege"):
            asyncio.run(validate())
    finally:
        with psycopg.connect(migrated_url, autocommit=True) as admin:
            admin.execute(SQL("DROP FUNCTION public.{}()").format(Identifier(public_function)))
