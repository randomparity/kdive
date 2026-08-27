"""Database authority boundaries for worker-incarnation artifact fences."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from queue import Queue
from threading import Event
from typing import Any, LiteralString
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier, Literal
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.db import migrate
from kdive.jobs import queue
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.ops import build_uses
from kdive.prereqs.system_bootstrap_key import (
    delete_system_bootstrap_key,
    ensure_system_bootstrap_key,
)
from kdive.reconciler.cleanup.artifact_retention import gc_expired_build_artifacts
from kdive.security.authz.rbac import PlatformRole, Role
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.runs.worker_incarnations import CURRENT_WORKER_FENCE_PROTOCOL
from tests.db import conftest as db_conftest
from tests.db_waits import DEFAULT_WAIT_TIMEOUT_S, wait_until_blocked_by

_LOGIN_AUTHENTICATION = "worker-fence-test-authentication"
_BINDING_MAX_BYTES = 4096
_PROTECTED_TABLES = {
    "capture_operation_cutoff",
    "capture_operations",
    "investigation_build_use_recoveries",
    "investigation_build_uses",
    "schema_migrations",
    "worker_incarnations",
}
_ORDINARY_TABLES = {
    "allocations",
    "artifacts",
    "audit_log",
    "budgets",
    "build_artifact_gc_cursors",
    "capture_reap_state",
    "component_uploads",
    "cost_class_coefficients",
    "debug_sessions",
    "egress_probe_guests",
    "host_dump_volume_leases",
    "idempotency_keys",
    "image_catalog",
    "investigation_build_gc_cursor",
    "investigation_build_tombstones",
    "investigation_builds",
    "investigations",
    "inventory_overrides",
    "jobs",
    "ledger",
    "object_write_leases",
    "ops_control",
    "platform_audit_log",
    "provider_components",
    "quotas",
    "resources",
    "rootfs_fetch_leases",
    "run_steps",
    "runs",
    "snapshots",
    "system_bootstrap_keys",
    "system_object_sweep_cursors",
    "system_shapes",
    "systems",
    "tool_invocation",
    "upload_manifests",
}
_SERVER_MUTATIONS = {
    "INSERT": _ORDINARY_TABLES
    - {
        "build_artifact_gc_cursors",
        "capture_reap_state",
        "host_dump_volume_leases",
        "investigation_build_gc_cursor",
        "investigation_build_tombstones",
        "system_object_sweep_cursors",
    },
    "UPDATE": _ORDINARY_TABLES
    - {
        "audit_log",
        "build_artifact_gc_cursors",
        "capture_reap_state",
        "host_dump_volume_leases",
        "investigation_build_gc_cursor",
        "investigation_build_tombstones",
        "ledger",
        "platform_audit_log",
        "system_object_sweep_cursors",
        "tool_invocation",
    },
    "DELETE": {
        "artifacts",
        "component_uploads",
        "debug_sessions",
        "egress_probe_guests",
        "idempotency_keys",
        "image_catalog",
        "inventory_overrides",
        "provider_components",
        "resources",
        "rootfs_fetch_leases",
        "run_steps",
        "snapshots",
        "system_bootstrap_keys",
        "system_shapes",
        "tool_invocation",
        "upload_manifests",
    },
}
_WORKER_SELECT = {
    "allocations",
    "artifacts",
    "budgets",
    "component_uploads",
    "cost_class_coefficients",
    "debug_sessions",
    "egress_probe_guests",
    "host_dump_volume_leases",
    "image_catalog",
    "investigation_build_tombstones",
    "investigation_builds",
    "investigations",
    "jobs",
    "ledger",
    "object_write_leases",
    "ops_control",
    "provider_components",
    "quotas",
    "resources",
    "rootfs_fetch_leases",
    "run_steps",
    "runs",
    "snapshots",
    "system_bootstrap_keys",
    "system_shapes",
    "systems",
    "upload_manifests",
}
_WORKER_MUTATIONS = {
    "INSERT": {
        "artifacts",
        "component_uploads",
        "egress_probe_guests",
        "host_dump_volume_leases",
        "ledger",
        "object_write_leases",
        "rootfs_fetch_leases",
        "run_steps",
        "snapshots",
        "system_bootstrap_keys",
        "upload_manifests",
    },
    "UPDATE": {
        "allocations",
        "artifacts",
        "budgets",
        "component_uploads",
        "debug_sessions",
        "egress_probe_guests",
        "image_catalog",
        "investigation_builds",
        "investigations",
        "run_steps",
        "runs",
        "snapshots",
        "systems",
        "upload_manifests",
    },
    "DELETE": {
        "artifacts",
        "host_dump_volume_leases",
        "object_write_leases",
        "rootfs_fetch_leases",
        "run_steps",
        "snapshots",
        "system_bootstrap_keys",
        "upload_manifests",
    },
}
_RECONCILER_SELECT = _ORDINARY_TABLES - {"audit_log", "platform_audit_log", "tool_invocation"}
_RECONCILER_MUTATIONS = {
    "INSERT": {
        "artifacts",
        "capture_reap_state",
        "cost_class_coefficients",
        "image_catalog",
        "investigation_build_tombstones",
        "inventory_overrides",
        "jobs",
        "ledger",
        "resources",
    },
    "UPDATE": {
        "allocations",
        "budgets",
        "build_artifact_gc_cursors",
        "capture_reap_state",
        "cost_class_coefficients",
        "debug_sessions",
        "egress_probe_guests",
        "image_catalog",
        "investigation_build_gc_cursor",
        "investigation_builds",
        "investigations",
        "jobs",
        "resources",
        "runs",
        "snapshots",
        "system_object_sweep_cursors",
        "systems",
        "upload_manifests",
    },
    "DELETE": {
        "artifacts",
        "host_dump_volume_leases",
        "idempotency_keys",
        "image_catalog",
        "investigation_builds",
        "inventory_overrides",
        "jobs",
        "object_write_leases",
        "resources",
        "rootfs_fetch_leases",
        "run_steps",
        "snapshots",
        "system_bootstrap_keys",
        "upload_manifests",
    },
}
_EXPECTED_ROLE_TABLE_PRIVILEGES = {
    "kdive_server": {"SELECT": _ORDINARY_TABLES, **_SERVER_MUTATIONS},
    "kdive_worker": {"SELECT": _WORKER_SELECT, **_WORKER_MUTATIONS},
    "kdive_reconciler": {"SELECT": _RECONCILER_SELECT, **_RECONCILER_MUTATIONS},
    "kdive_lifecycle_witness": {},
    "unprivileged": {},
}


@dataclass(frozen=True)
class RoleDsns:
    """Connection strings and LOGIN names for the isolated runtime principals."""

    parameters: dict[str, str]
    logins: dict[str, str]

    def __call__(self, role: str) -> str:
        parameters = {
            **self.parameters,
            "user": self.logins[role],
            "password": _LOGIN_AUTHENTICATION,
        }
        return make_conninfo(**parameters)


class _RecordingGenerationStore:
    """Record exact generation-version deletion attempted by the real GC path."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_version(self, key: str, version_id: str) -> None:
        self.deleted.append(f"{key}@{version_id}")

    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        return True


@dataclass(frozen=True)
class _PaginatedUses:
    blocked: tuple[UUID, ...]
    recoverable: UUID
    foreign: UUID
    recoverable_holder: str


def _operator_context(
    *,
    platform_operator: bool = True,
    project: str | None = "project-a",
    project_role: Role | None = Role.VIEWER,
) -> RequestContext:
    projects = () if project is None else (project,)
    roles = {} if project is None or project_role is None else {project: project_role}
    return RequestContext(
        principal="operator-1",
        agent_session="session-1",
        projects=projects,
        roles=roles,
        platform_roles=(
            frozenset({PlatformRole.PLATFORM_OPERATOR}) if platform_operator else frozenset()
        ),
    )


@pytest.fixture
def role_dsn(pg_conn: psycopg.Connection) -> Iterator[RoleDsns]:
    """Create one uniquely named LOGIN principal for each non-login runtime role."""
    migrate.apply_migrations(pg_conn)
    database_suffix = pg_conn.info.dbname[-16:].replace("-", "_")
    logins = {
        "kdive_server": f"kdive_server_{database_suffix}",
        "kdive_worker": f"kdive_worker_{database_suffix}",
        "kdive_reconciler": f"kdive_reconciler_{database_suffix}",
        "kdive_lifecycle_witness": f"kdive_witness_{database_suffix}",
        "unprivileged": f"kdive_unprivileged_{database_suffix}",
    }
    for login in logins.values():
        pg_conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(login)))
    for role, login in logins.items():
        membership = (
            SQL("") if role == "unprivileged" else SQL(" IN ROLE {}").format(Identifier(role))
        )
        pg_conn.execute(
            SQL("CREATE ROLE {} LOGIN PASSWORD {}{}").format(
                Identifier(login), Literal(_LOGIN_AUTHENTICATION), membership
            )
        )

    yield RoleDsns(dict(pg_conn.info.get_parameters()), logins)

    for login in logins.values():
        pg_conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(login)))


@pytest.fixture
def residual_privilege_role_dsn(pg_conn: psycopg.Connection) -> Iterator[RoleDsns]:
    """Apply the authority migrations over compatible roles carrying residual grants."""
    suffix = pg_conn.info.dbname[-10:].replace("-", "_")
    roles = {
        "kdive_server": f"kdive_acl_server_{suffix}",
        "kdive_worker": f"kdive_acl_worker_{suffix}",
        "kdive_reconciler": f"kdive_acl_reconciler_{suffix}",
        "kdive_lifecycle_witness": f"kdive_acl_witness_{suffix}",
    }
    logins = {role: f"{isolated}_login" for role, isolated in roles.items()}
    role_list = SQL(", ").join(Identifier(role) for role in roles.values())
    for role in roles.values():
        pg_conn.execute(
            SQL(
                "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(Identifier(role))
        )
    for canonical, login in logins.items():
        pg_conn.execute(
            SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                Identifier(login),
                Literal(_LOGIN_AUTHENTICATION),
                Identifier(roles[canonical]),
            )
        )

    try:
        pg_conn.execute(SQL("ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO {}").format(role_list))
        pg_conn.execute(
            SQL("ALTER DEFAULT PRIVILEGES GRANT EXECUTE ON FUNCTIONS TO {}").format(role_list)
        )
        for migration in migrate.discover_migrations():
            pg_conn.execute(migration.sql.encode())
            if migration.version == "0103":
                break
        pg_conn.execute(
            SQL(
                "GRANT ALL ON TABLE worker_incarnations, investigation_build_uses, "
                "investigation_build_use_recoveries TO {}"
            ).format(role_list)
        )
        for filename in (
            "0104_worker_fence_roles.sql",
            "0105_worker_fence_functions.sql",
            "0106_worker_fence_protocol_claim.sql",
            "0108_worker_fence_runtime_paths.sql",
            "0109_kubernetes_credential_envelopes.sql",
            "0110_idempotent_worker_termination.sql",
            "0111_restrict_pinned_job_deletion.sql",
            "0112_capture_operation_supervision.sql",
            "0113_capture_publication_fence.sql",
            "0116_capture_claimable_queue_depth.sql",
        ):
            role_sql = (migrate.SCHEMA_DIR / filename).read_bytes()
            for canonical, isolated in roles.items():
                role_sql = role_sql.replace(canonical.encode(), isolated.encode())
            pg_conn.execute(role_sql)
        yield RoleDsns(dict(pg_conn.info.get_parameters()), logins)
    finally:
        pg_conn.execute(
            SQL("ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM {}").format(role_list)
        )
        pg_conn.execute(
            SQL("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM {}").format(role_list)
        )
        for login in logins.values():
            pg_conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(login)))
        for role in roles.values():
            pg_conn.execute(SQL("DROP OWNED BY {}").format(Identifier(role)))
            pg_conn.execute(SQL("DROP ROLE {}").format(Identifier(role)))


def _login_operation_succeeds(conn: psycopg.Connection, operation: str) -> bool:
    try:
        if operation == "direct_terminate":
            conn.execute("UPDATE worker_incarnations SET state = 'terminated'")
        elif operation == "register":
            conn.execute(
                "SELECT public.register_worker_incarnation(%s, %s, %s, %s, %s)",
                (
                    "docker:authority-test",
                    "docker",
                    Jsonb({"container_id": "a" * 64}),
                    bytes(32),
                    CURRENT_WORKER_FENCE_PROTOCOL,
                ),
            )
        elif operation == "terminate_function":
            conn.execute(
                "SELECT public.terminate_worker_incarnation(%s, %s, %s, %s)",
                ("missing", "docker", Jsonb({"container_id": "a" * 64}), "failed"),
            )
        elif operation == "direct_delete_use":
            conn.execute("DELETE FROM investigation_build_uses")
        else:  # pragma: no cover - parameterization owns the operation names.
            raise AssertionError(f"unknown operation {operation}")
    except psycopg.Error:
        conn.rollback()
        return False
    return True


def _register(
    role_dsn: RoleDsns,
    incarnation: str,
    credential_hash: bytes,
    *,
    binding: dict[str, object] | None = None,
) -> None:
    with psycopg.connect(role_dsn("kdive_lifecycle_witness"), autocommit=True) as conn:
        conn.execute(
            "SELECT public.register_worker_incarnation(%s, %s, %s, %s, %s)",
            (
                incarnation,
                "docker",
                Jsonb(binding or {"container_id": "a" * 64}),
                credential_hash,
                CURRENT_WORKER_FENCE_PROTOCOL,
            ),
        )


def _seed_claim(
    conn: psycopg.Connection,
    *,
    holder: str,
    project: str = "project-a",
    attempt: int = 1,
) -> tuple[UUID, UUID, UUID]:
    investigation_id, generation, run_id, job_id = uuid4(), uuid4(), uuid4(), uuid4()
    digest = "d" * 64
    build_ref = f"{digest}.{generation}"
    conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'principal', %s, 'title', 'active')",
        (investigation_id, project),
    )
    conn.execute(
        "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
        "content_digest, canonical_document, build_result, artifacts, target_kind, "
        "build_profile, expires_at) VALUES "
        "(%s, %s, %s, %s, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, "
        "'local-libvirt', '{}'::jsonb, now() + interval '1 day')",
        (investigation_id, generation, build_ref, digest),
    )
    conn.execute(
        "INSERT INTO runs (id, investigation_id, state, build_profile, target_kind, "
        "principal, project, build_ref) VALUES "
        "(%s, %s, 'running', '{}'::jsonb, 'local-libvirt', 'principal', %s, %s)",
        (run_id, investigation_id, project, build_ref),
    )
    conn.execute(
        "INSERT INTO jobs (id, kind, state, attempt, max_attempts, worker_id, "
        "lease_expires_at, payload, authorizing, dedup_key) VALUES "
        "(%s, 'install', 'running', %s, 3, %s, now() + interval '5 minutes', "
        "%s, %s, %s)",
        (
            job_id,
            attempt,
            holder,
            Jsonb({"run_id": str(run_id)}),
            Jsonb({"principal": "principal", "project": project}),
            f"worker-fence-{job_id}",
        ),
    )
    return investigation_id, generation, job_id


def _seed_queued_job(conn: psycopg.Connection) -> UUID:
    job_id = uuid4()
    conn.execute(
        "INSERT INTO jobs (id, kind, state, max_attempts, payload, authorizing, dedup_key) "
        "VALUES (%s, 'install', 'queued', 3, '{}'::jsonb, %s, %s)",
        (
            job_id,
            Jsonb({"principal": "principal", "agent_session": None, "project": "project-a"}),
            f"worker-fence-lease-{job_id}",
        ),
    )
    return job_id


def _acquire(
    role_dsn: RoleDsns,
    use_id: UUID,
    investigation_id: UUID,
    generation: UUID,
    job_id: UUID,
    attempt: int,
    credential_hash: bytes,
) -> bool:
    with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as conn:
        row = conn.execute(
            "SELECT public.acquire_investigation_build_use(%s, %s, %s, %s, %s, %s)",
            (use_id, investigation_id, generation, job_id, attempt, credential_hash),
        ).fetchone()
    assert row is not None
    return bool(row[0])


def _seed_paginated_uses(pg_conn: psycopg.Connection, role_dsn: RoleDsns) -> _PaginatedUses:
    """Seed 100 blocked pins, one later recoverable pin, and one foreign pin."""
    blocked_holder = "docker:page-blocked"
    recoverable_holder = "docker:page-recoverable"
    foreign_holder = "docker:page-foreign"
    for holder, credential in (
        (blocked_holder, b"b" * 32),
        (recoverable_holder, b"r" * 32),
        (foreign_holder, b"f" * 32),
    ):
        _register(role_dsn, holder, credential)

    blocked_inv, blocked_generation, blocked_job = _seed_claim(
        pg_conn, holder=blocked_holder, project="project-a"
    )
    recoverable_inv, recoverable_generation, recoverable_job = _seed_claim(
        pg_conn, holder=recoverable_holder, project="project-a"
    )
    foreign_inv, foreign_generation, foreign_job = _seed_claim(
        pg_conn, holder=foreign_holder, project="project-b"
    )
    local_ids = sorted(uuid4() for _ in range(101))
    blocked, recoverable = tuple(local_ids[:100]), local_ids[100]
    foreign = uuid4()
    created_at = datetime.now(UTC) - timedelta(days=1)
    rows = [
        (
            use_id,
            blocked_inv,
            blocked_generation,
            blocked_job,
            blocked_holder,
            created_at,
        )
        for use_id in blocked
    ]
    rows.extend(
        (
            use_id,
            investigation_id,
            generation,
            job_id,
            holder,
            created_at,
        )
        for use_id, investigation_id, generation, job_id, holder in (
            (
                recoverable,
                recoverable_inv,
                recoverable_generation,
                recoverable_job,
                recoverable_holder,
            ),
            (foreign, foreign_inv, foreign_generation, foreign_job, foreign_holder),
        )
    )
    with pg_conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO investigation_build_uses "
            "(use_id, investigation_id, generation, job_id, attempt, holder_worker_id, "
            "lease_expires_at, created_at) VALUES (%s, %s, %s, %s, 1, %s, "
            "now() + interval '5 minutes', %s)",
            rows,
        )
    with psycopg.connect(role_dsn("kdive_lifecycle_witness"), autocommit=True) as witness:
        assert witness.execute(
            "SELECT public.terminate_worker_incarnation(%s, 'docker', %s, 'killed')",
            (recoverable_holder, Jsonb({"container_id": "a" * 64})),
        ).fetchone() == (True,)
    return _PaginatedUses(blocked, recoverable, foreign, recoverable_holder)


def test_server_role_lists_build_uses_through_the_operator_tool(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """The deployed server DSN can run bounded diagnostics without protected-table reads."""
    holder, credential = "docker:list-role-path", b"l" * 32
    foreign_holder, foreign_credential = "docker:list-role-foreign", b"f" * 32
    _register(role_dsn, holder, credential)
    _register(role_dsn, foreign_holder, foreign_credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder, project="project-a")
    foreign_investigation, foreign_generation, foreign_job = _seed_claim(
        pg_conn, holder=foreign_holder, project="project-b"
    )
    use_id, foreign_use = uuid4(), uuid4()
    assert _acquire(
        role_dsn,
        use_id,
        investigation_id,
        generation,
        job_id,
        1,
        credential,
    )
    assert _acquire(
        role_dsn,
        foreign_use,
        foreign_investigation,
        foreign_generation,
        foreign_job,
        1,
        foreign_credential,
    )

    async def exercise() -> None:
        pool = AsyncConnectionPool(role_dsn("kdive_server"), min_size=1, max_size=1, open=False)
        await pool.open()
        try:
            denied = await build_uses.list_build_uses(
                pool, _operator_context(platform_operator=False), limit=100
            )
            assert denied.error_category == "authorization_denied"
            assert denied.items == []

            platform_only = await build_uses.list_build_uses(
                pool, _operator_context(project=None), limit=100
            )
            assert platform_only.status == "ok"
            assert platform_only.items == []

            membership_without_role = await build_uses.list_build_uses(
                pool, _operator_context(project_role=None), limit=100
            )
            assert membership_without_role.status == "ok"
            assert membership_without_role.items == []

            listed = await build_uses.list_build_uses(pool, _operator_context(), limit=10_000)
            assert listed.status == "ok"
            assert listed.data["limit"] == 100
            assert [item.object_id for item in listed.items] == [str(use_id)]
            assert listed.items[0].data == {
                "investigation_id": str(investigation_id),
                "generation": str(generation),
                "job_id": str(job_id),
                "attempt": "1",
                "holder": holder,
                "created_at": listed.items[0].data["created_at"],
            }
        finally:
            await pool.close()

    asyncio.run(exercise())


def test_server_role_pages_past_blocked_pins_without_cross_tenant_leak(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """A blocked first page cannot hide a later recoverable pin in the caller's tenant."""
    uses = _seed_paginated_uses(pg_conn, role_dsn)

    async def exercise() -> None:
        pool = AsyncConnectionPool(role_dsn("kdive_server"), min_size=1, max_size=1, open=False)
        await pool.open()
        try:
            first = await build_uses.list_build_uses(pool, _operator_context(), limit=100)
            assert [item.object_id for item in first.items] == [str(uid) for uid in uses.blocked]
            assert first.data["truncated"] is True
            cursor = first.data["next_cursor"]
            assert isinstance(cursor, str)

            blocked = await build_uses.recover_build_use(
                pool,
                _operator_context(),
                use_id=uses.blocked[0],
                holder="docker:page-blocked",
                reason="prove the active prefix is not recoverable",
            )
            assert blocked.error_category == "configuration_error"

            second = await build_uses.list_build_uses(
                pool, _operator_context(), limit=100, cursor=cursor
            )
            assert [item.object_id for item in second.items] == [str(uses.recoverable)]
            assert second.data["truncated"] is False
            assert second.data["next_cursor"] is None
            assert all(item.object_id != str(uses.foreign) for item in first.items + second.items)

            recovered = await build_uses.recover_build_use(
                pool,
                _operator_context(),
                use_id=uses.recoverable,
                holder=uses.recoverable_holder,
                reason="reached through the continuation page",
            )
            assert recovered.status == "recovered"

            terminal = await build_uses.list_build_uses(
                pool, _operator_context(), limit=100, cursor=cursor
            )
            assert terminal.items == []
            assert terminal.data["truncated"] is False
            assert terminal.data["next_cursor"] is None

        finally:
            await pool.close()

    asyncio.run(exercise())
    assert pg_conn.execute(
        "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s",
        (uses.foreign,),
    ).fetchone() == (1,)


def test_server_role_recovers_one_exact_build_use_through_the_operator_tool(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """The server request and platform audit wrap the exact evidence-checked SQL transition."""
    holder, credential = "docker:recover-role-path", b"r" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder, project="project-b")
    use_id = uuid4()
    assert _acquire(
        role_dsn,
        use_id,
        investigation_id,
        generation,
        job_id,
        1,
        credential,
    )
    with psycopg.connect(role_dsn("kdive_lifecycle_witness"), autocommit=True) as witness:
        assert witness.execute(
            "SELECT public.terminate_worker_incarnation(%s, 'docker', %s, 'killed')",
            (holder, Jsonb({"container_id": "a" * 64})),
        ).fetchone() == (True,)

    async def exercise() -> None:
        pool = AsyncConnectionPool(role_dsn("kdive_server"), min_size=1, max_size=1, open=False)
        await pool.open()
        try:
            denied = await build_uses.recover_build_use(
                pool,
                _operator_context(platform_operator=False),
                use_id=use_id,
                holder=holder,
                reason="attempted cross-project recovery",
            )
            assert denied.error_category == "authorization_denied"
            assert pg_conn.execute(
                "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s", (use_id,)
            ).fetchone() == (1,)
            cross_project = await build_uses.recover_build_use(
                pool,
                _operator_context(),
                use_id=use_id,
                holder=holder,
                reason="confirmed lifecycle termination",
            )
            missing = await build_uses.recover_build_use(
                pool,
                _operator_context(),
                use_id=uuid4(),
                holder=holder,
                reason="confirmed lifecycle termination",
            )
            assert cross_project.error_category == "configuration_error"
            assert missing.error_category == cross_project.error_category
            assert missing.status == cross_project.status
            assert missing.detail == cross_project.detail
            assert pg_conn.execute(
                "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s", (use_id,)
            ).fetchone() == (1,)

            platform_only = await build_uses.recover_build_use(
                pool,
                _operator_context(project=None),
                use_id=use_id,
                holder=holder,
                reason="confirmed lifecycle termination",
            )
            assert platform_only.error_category == cross_project.error_category
            assert platform_only.status == cross_project.status
            assert platform_only.detail == cross_project.detail
            recovered = await build_uses.recover_build_use(
                pool,
                _operator_context(project="project-b"),
                use_id=use_id,
                holder=holder,
                reason="confirmed lifecycle termination",
            )
            assert recovered.status == "recovered"
        finally:
            await pool.close()

    asyncio.run(exercise())
    assert pg_conn.execute(
        "SELECT project, investigation_id, generation, holder_worker_id, recovered_by, reason "
        "FROM investigation_build_use_recoveries WHERE use_id = %s",
        (use_id,),
    ).fetchone() == (
        "project-b",
        investigation_id,
        generation,
        holder,
        "operator-1",
        "confirmed lifecycle termination",
    )
    assert pg_conn.execute(
        "SELECT count(*) FROM platform_audit_log WHERE tool = 'ops.recover_build_use'"
    ).fetchone() == (4,)


def test_reconciler_cannot_delete_job_while_build_use_is_pinned(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Routine job cleanup cannot erase a live pin or forge recovery evidence."""
    holder, credential = "docker:job-delete-blocked", b"b" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder)
    use_id = uuid4()
    assert _acquire(
        role_dsn,
        use_id,
        investigation_id,
        generation,
        job_id,
        1,
        credential,
    )

    with (
        psycopg.connect(role_dsn("kdive_reconciler"), autocommit=True) as reconciler,
        pytest.raises(psycopg.errors.ForeignKeyViolation),
    ):
        reconciler.execute("DELETE FROM jobs WHERE id = %s", (job_id,))

    assert pg_conn.execute("SELECT count(*) FROM jobs WHERE id = %s", (job_id,)).fetchone() == (1,)
    assert pg_conn.execute(
        "SELECT job_id, holder_worker_id FROM investigation_build_uses WHERE use_id = %s",
        (use_id,),
    ).fetchone() == (job_id, holder)
    assert pg_conn.execute(
        "SELECT count(*) FROM investigation_build_use_recoveries WHERE use_id = %s",
        (use_id,),
    ).fetchone() == (0,)


def test_reconciler_can_delete_job_after_worker_releases_build_use(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """An ordinary worker release removes the pin before routine job cleanup."""
    holder, credential = "docker:job-delete-released", b"r" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder)
    use_id = uuid4()
    assert _acquire(
        role_dsn,
        use_id,
        investigation_id,
        generation,
        job_id,
        1,
        credential,
    )

    with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker:
        released = worker.execute(
            "SELECT public.release_investigation_build_use(%s, %s)",
            (use_id, credential),
        ).fetchone()
    assert released == (True,)
    with psycopg.connect(role_dsn("kdive_reconciler"), autocommit=True) as reconciler:
        reconciler.execute("DELETE FROM jobs WHERE id = %s", (job_id,))

    assert pg_conn.execute("SELECT count(*) FROM jobs WHERE id = %s", (job_id,)).fetchone() == (0,)
    assert pg_conn.execute(
        "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s", (use_id,)
    ).fetchone() == (0,)


def test_reconciler_can_delete_job_after_evidence_recovery(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Evidence recovery removes the pin while preserving its immutable audit row."""
    holder, credential = "docker:job-delete-recovered", b"e" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder)
    use_id = uuid4()
    assert _acquire(
        role_dsn,
        use_id,
        investigation_id,
        generation,
        job_id,
        1,
        credential,
    )
    with psycopg.connect(role_dsn("kdive_lifecycle_witness"), autocommit=True) as witness:
        assert witness.execute(
            "SELECT public.terminate_worker_incarnation(%s, 'docker', %s, 'killed')",
            (holder, Jsonb({"container_id": "a" * 64})),
        ).fetchone() == (True,)
    with psycopg.connect(role_dsn("kdive_reconciler"), autocommit=True) as reconciler:
        assert reconciler.execute(
            "SELECT public.recover_investigation_build_use(%s, %s::text[], %s, %s, %s)",
            (use_id, ["project-a"], holder, "reconciler:test", "worker terminated"),
        ).fetchone() == (True,)
        reconciler.execute("DELETE FROM jobs WHERE id = %s", (job_id,))

    assert pg_conn.execute("SELECT count(*) FROM jobs WHERE id = %s", (job_id,)).fetchone() == (0,)
    assert pg_conn.execute(
        "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s", (use_id,)
    ).fetchone() == (0,)
    assert pg_conn.execute(
        "SELECT job_id, holder_worker_id FROM investigation_build_use_recoveries WHERE use_id = %s",
        (use_id,),
    ).fetchone() == (job_id, holder)


def test_reconciler_role_generation_gc_honors_exact_use_pins(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """The deployed reconciler DSN can reclaim only an unpinned exact generation."""
    holder = "docker:gc-role-path"
    _register(role_dsn, holder, b"g" * 32)
    pinned_investigation, eligible_investigation = uuid4(), uuid4()
    pinned_generation, eligible_generation = uuid4(), uuid4()
    pinned_job, pinned_use = uuid4(), uuid4()
    for investigation_id, project in (
        (pinned_investigation, "project-a"),
        (eligible_investigation, "project-b"),
    ):
        pg_conn.execute(
            "INSERT INTO investigations (id, principal, project, title, state) "
            "VALUES (%s, 'principal', %s, 'title', 'active')",
            (investigation_id, project),
        )
    for investigation_id, generation, digest, key in (
        (pinned_investigation, pinned_generation, "a" * 64, "builds/pinned/kernel"),
        (eligible_investigation, eligible_generation, "b" * 64, "builds/eligible/kernel"),
    ):
        pg_conn.execute(
            "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
            "content_digest, canonical_document, build_result, artifacts, target_kind, "
            "build_profile, expires_at) VALUES (%s, %s, %s, %s, '{}'::jsonb, '{}'::jsonb, "
            "%s, 'local-libvirt', '{}'::jsonb, now() - interval '1 second')",
            (
                investigation_id,
                generation,
                f"{digest}.{generation}",
                digest,
                Jsonb({"kernel": {"key": key, "version_id": f"v-{generation}"}}),
            ),
        )
    pg_conn.execute(
        "INSERT INTO jobs (id, kind, state, attempt, max_attempts, worker_id, "
        "lease_expires_at, authorizing, dedup_key) VALUES "
        "(%s, 'install', 'running', 1, 3, %s, now() + interval '5 minutes', "
        "'{}'::jsonb, %s)",
        (pinned_job, holder, f"gc-role-{pinned_job}"),
    )
    pg_conn.execute(
        "INSERT INTO investigation_build_uses (use_id, investigation_id, generation, job_id, "
        "attempt, holder_worker_id, lease_expires_at) VALUES "
        "(%s, %s, %s, %s, 1, %s, now() + interval '5 minutes')",
        (pinned_use, pinned_investigation, pinned_generation, pinned_job, holder),
    )

    async def exercise() -> list[str]:
        store = _RecordingGenerationStore()
        conn = await psycopg.AsyncConnection.connect(role_dsn("kdive_reconciler"), autocommit=True)
        try:
            assert await gc_expired_build_artifacts(conn, store, timedelta(days=30)) == 1
        finally:
            await conn.close()
        return store.deleted

    assert asyncio.run(exercise()) == [f"builds/eligible/kernel@v-{eligible_generation}"]
    assert pg_conn.execute(
        "SELECT generation FROM investigation_builds ORDER BY generation"
    ).fetchall() == [(pinned_generation,)]


@pytest.mark.parametrize(
    ("role", "operation", "allowed"),
    [
        ("kdive_worker", "direct_terminate", False),
        ("kdive_lifecycle_witness", "register", True),
        ("kdive_worker", "terminate_function", False),
        ("kdive_reconciler", "direct_delete_use", False),
        ("unprivileged", "register", False),
    ],
)
def test_worker_fence_role_matrix(
    role: str, operation: str, allowed: bool, role_dsn: RoleDsns
) -> None:
    """Only the witness can record a worker incarnation through its bounded API."""
    with psycopg.connect(role_dsn(role), autocommit=True) as role_conn:
        assert _login_operation_succeeds(role_conn, operation) is allowed


def test_worker_fence_roles_and_login_memberships_are_exact(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Runtime capabilities cannot log in or inherit escalation roles."""
    runtime_roles = {
        "kdive_server",
        "kdive_worker",
        "kdive_reconciler",
        "kdive_lifecycle_witness",
    }
    rows = pg_conn.execute(
        "SELECT rolname, rolcanlogin, rolinherit, rolsuper, rolcreaterole, rolcreatedb, "
        "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
        (list(runtime_roles),),
    ).fetchall()
    assert {row[0] for row in rows} == runtime_roles
    assert all(row[1:] == (False, False, False, False, False, False, False) for row in rows)

    escalation_memberships = pg_conn.execute(
        "SELECT member_role.rolname, granted_role.rolname FROM pg_auth_members m "
        "JOIN pg_roles member_role ON member_role.oid = m.member "
        "JOIN pg_roles granted_role ON granted_role.oid = m.roleid "
        "WHERE member_role.rolname = ANY(%s)",
        (list(runtime_roles),),
    ).fetchall()
    assert escalation_memberships == []

    login_memberships = pg_conn.execute(
        "SELECT member_role.rolname, array_agg(granted_role.rolname ORDER BY granted_role.rolname) "
        "FROM pg_roles member_role "
        "LEFT JOIN pg_auth_members m ON m.member = member_role.oid "
        "LEFT JOIN pg_roles granted_role ON granted_role.oid = m.roleid "
        "WHERE member_role.rolname = ANY(%s) GROUP BY member_role.rolname",
        (list(role_dsn.logins.values()),),
    ).fetchall()
    expected = {
        login: ([role] if role != "unprivileged" else [None])
        for role, login in role_dsn.logins.items()
    }
    assert dict(login_memberships) == expected


def test_migration_upgrade_scrubs_residual_table_mutation_grants(
    residual_privilege_role_dsn: RoleDsns,
) -> None:
    """Compatible runtime roles lose explicit and default protected-table mutation grants."""
    tables = [
        "worker_incarnations",
        "investigation_build_uses",
        "investigation_build_use_recoveries",
    ]
    for role in residual_privilege_role_dsn.logins:
        with psycopg.connect(residual_privilege_role_dsn(role), autocommit=True) as runtime:
            for table in tables:
                for privilege in ("INSERT", "UPDATE", "DELETE"):
                    effective = runtime.execute(
                        "SELECT has_table_privilege(current_user, %s, %s)",
                        (table, privilege),
                    ).fetchone()
                    assert effective == (False,)

    denied_mutations: dict[str, LiteralString] = {
        "kdive_server": "DELETE FROM worker_incarnations",
        "kdive_worker": "DELETE FROM investigation_build_uses",
        "kdive_reconciler": "DELETE FROM investigation_build_use_recoveries",
        "kdive_lifecycle_witness": "UPDATE worker_incarnations SET state = 'terminated'",
    }
    for role, operation in denied_mutations.items():
        with (
            psycopg.connect(residual_privilege_role_dsn(role), autocommit=True) as runtime,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            runtime.execute(SQL(operation))


def test_runtime_roles_receive_data_access_without_crossing_fence_authority(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Runtime processes can use ordinary data while guarded evidence stays API-only."""
    ordinary_tables = {
        str(row[0])
        for row in pg_conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        ).fetchall()
    } - _PROTECTED_TABLES
    sequences = {
        str(row[0])
        for row in pg_conn.execute(
            "SELECT sequence_name FROM information_schema.sequences "
            "WHERE sequence_schema = 'public'"
        ).fetchall()
    }
    assert ordinary_tables == _ORDINARY_TABLES

    for role, login in role_dsn.logins.items():
        expected_by_operation = _EXPECTED_ROLE_TABLE_PRIVILEGES[role]
        for table in ordinary_tables | _PROTECTED_TABLES:
            for privilege in (
                "DELETE",
                "INSERT",
                "REFERENCES",
                "SELECT",
                "TRIGGER",
                "TRUNCATE",
                "UPDATE",
            ):
                effective = pg_conn.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (login, f"public.{table}", privilege),
                ).fetchone()
                assert effective == (table in expected_by_operation.get(privilege, set()),), (
                    role,
                    table,
                    privilege,
                )
        for sequence in sequences:
            privileges = pg_conn.execute(
                "SELECT has_sequence_privilege(%s, %s, privilege) "
                "FROM unnest(%s::text[]) AS privilege ORDER BY privilege",
                (login, f"public.{sequence}", ["SELECT", "UPDATE", "USAGE"]),
            ).fetchall()
            assert privileges == [(False,)] * 3, (role, sequence)


def test_worker_bootstrap_key_path_has_exact_role_authority(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """The worker creates, reads, and deletes its key; unrelated runtimes cannot create one."""
    resource_id, allocation_id, system_id = uuid4(), uuid4(), uuid4()
    pg_conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'default', 'standard', 'available', 'qemu:///system')",
        (resource_id,),
    )
    pg_conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'granted', 'principal', 'project')",
        (allocation_id, resource_id),
    )
    pg_conn.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, %s, 'ready', '{}'::jsonb, 'principal', 'project')",
        (system_id, allocation_id),
    )

    for role in ("kdive_reconciler", "kdive_lifecycle_witness", "unprivileged"):
        with (
            psycopg.connect(role_dsn(role), autocommit=True) as runtime,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            runtime.execute(
                "INSERT INTO system_bootstrap_keys (system_id, private_key, public_key) "
                "VALUES (%s, 'private', 'ssh-ed25519 denied')",
                (system_id,),
            )

    async def exercise_worker_path() -> None:
        async with await psycopg.AsyncConnection.connect(
            role_dsn("kdive_worker"), autocommit=True
        ) as worker:
            public_key = await ensure_system_bootstrap_key(
                worker, system_id, secret_registry=SecretRegistry()
            )
            stored = await (
                await worker.execute(
                    "SELECT public_key FROM system_bootstrap_keys WHERE system_id = %s",
                    (system_id,),
                )
            ).fetchone()
            assert stored == (public_key,)

            await delete_system_bootstrap_key(worker, system_id)
            remaining = await (
                await worker.execute(
                    "SELECT count(*) FROM system_bootstrap_keys WHERE system_id = %s",
                    (system_id,),
                )
            ).fetchone()
            assert remaining == (0,)

    asyncio.run(exercise_worker_path())


@pytest.mark.parametrize(
    "operation",
    [
        "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
        "VALUES ('install', '{}'::jsonb, 'queued', 3, '{}'::jsonb, 'worker-bypass')",
        "UPDATE jobs SET state = 'running', worker_id = 'observed-current-worker'",
        "UPDATE jobs SET state = 'failed' WHERE state = 'running'",
        "DELETE FROM jobs",
    ],
)
def test_worker_role_cannot_mutate_jobs_directly(
    role_dsn: RoleDsns, operation: LiteralString
) -> None:
    """A worker login cannot bypass credential-gated job transitions with table DML."""
    with (
        psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        worker.execute(SQL(operation))


def test_worker_job_functions_fence_credential_holder_and_attempt(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Guarded job writes derive the holder and match the exact charged attempt."""
    holder_a, holder_b = "docker:job-owner-a", "docker:job-owner-b"
    credential_a, credential_b = b"a" * 32, b"b" * 32
    _register(role_dsn, holder_a, credential_a)
    _register(role_dsn, holder_b, credential_b)

    _investigation, _generation, heartbeat_job = _seed_claim(pg_conn, holder=holder_a, attempt=1)
    _investigation, _generation, complete_job = _seed_claim(pg_conn, holder=holder_a, attempt=2)
    _investigation, _generation, requeue_job = _seed_claim(pg_conn, holder=holder_a, attempt=1)
    _investigation, _generation, fail_job = _seed_claim(pg_conn, holder=holder_a, attempt=3)

    with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker:
        assert worker.execute(
            "SELECT public.heartbeat_worker_job(%s, %s, 1, interval '5 minutes')",
            (heartbeat_job, credential_a),
        ).fetchone() == (True,)
        assert worker.execute(
            "SELECT public.heartbeat_worker_job(%s, %s, 2, interval '5 minutes')",
            (heartbeat_job, credential_a),
        ).fetchone() == (False,)
        assert (
            worker.execute(
                "SELECT public.complete_worker_job(%s, %s, 2, 'result-a')",
                (complete_job, credential_b),
            ).fetchone()
            is None
        )
        completed = worker.execute(
            "SELECT state, result_ref FROM public.complete_worker_job(%s, %s, 2, 'result-a')",
            (complete_job, credential_a),
        ).fetchone()
        assert completed == ("succeeded", "result-a")
        requeued = worker.execute(
            "SELECT state, worker_id FROM public.fail_worker_job("
            "%s, %s, 1, 'infrastructure_failure', '{}'::jsonb, false)",
            (requeue_job, credential_a),
        ).fetchone()
        assert requeued == ("queued", None)
        failed = worker.execute(
            "SELECT state, error_category FROM public.fail_worker_job("
            "%s, %s, 3, 'build_failure', '{\"failure_message\":\"bounded\"}'::jsonb, false)",
            (fail_job, credential_a),
        ).fetchone()
        assert failed == ("failed", "build_failure")


@pytest.mark.parametrize(
    "lease",
    [
        "0 seconds",
        "-1 microsecond",
        "1 hour 1 microsecond",
        pytest.param("1000000 years", id="timestamp-overflow"),
        pytest.param("-1 year 360 days 30 minutes", id="calendar-expired"),
        pytest.param("1 year -360 days 30 minutes", id="calendar-over-limit"),
    ],
)
def test_worker_claim_rejects_out_of_contract_lease_without_state_change(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns, lease: str
) -> None:
    """Hostile worker SQL cannot claim with an expired or over-limit lease."""
    holder, credential = "docker:bounded-claim", b"c" * 32
    _register(role_dsn, holder, credential)
    job_id = _seed_queued_job(pg_conn)

    with (
        psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker,
        pytest.raises(psycopg.errors.InvalidParameterValue, match="lease"),
    ):
        worker.execute(
            "SELECT * FROM public.claim_worker_job(%s, %s, %s::interval, %s::text[])",
            (holder, credential, lease, ["default"]),
        )

    assert pg_conn.execute(
        "SELECT state, attempt, worker_id, lease_expires_at, heartbeat_at FROM jobs WHERE id = %s",
        (job_id,),
    ).fetchone() == ("queued", 0, None, None, None)


@pytest.mark.parametrize(
    "lease",
    [
        "0 seconds",
        "-1 microsecond",
        "1 hour 1 microsecond",
        pytest.param("1000000 years", id="timestamp-overflow"),
        pytest.param("-1 year 360 days 30 minutes", id="calendar-expired"),
        pytest.param("1 year -360 days 30 minutes", id="calendar-over-limit"),
    ],
)
def test_worker_heartbeat_rejects_out_of_contract_lease_without_state_change(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns, lease: str
) -> None:
    """Hostile worker SQL cannot expire or overextend an owned running attempt."""
    holder, credential = "docker:bounded-heartbeat", b"h" * 32
    _register(role_dsn, holder, credential)
    _investigation, _generation, job_id = _seed_claim(pg_conn, holder=holder, attempt=1)
    before = pg_conn.execute(
        "SELECT state, attempt, worker_id, lease_expires_at, heartbeat_at FROM jobs WHERE id = %s",
        (job_id,),
    ).fetchone()

    with (
        psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker,
        pytest.raises(psycopg.errors.InvalidParameterValue, match="lease"),
    ):
        worker.execute(
            "SELECT public.heartbeat_worker_job(%s, %s, 1, %s::interval)",
            (job_id, credential, lease),
        )

    assert (
        pg_conn.execute(
            "SELECT state, attempt, worker_id, lease_expires_at, heartbeat_at "
            "FROM jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
        == before
    )


def test_worker_claim_and_heartbeat_accept_one_hour_lease_ceiling(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """The one-hour lease ceiling is inclusive for current credential-bound paths."""
    holder, credential = "docker:max-valid-lease", b"m" * 32
    _register(role_dsn, holder, credential)
    job_id = _seed_queued_job(pg_conn)

    with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker:
        claimed = worker.execute(
            "SELECT state, attempt FROM public.claim_worker_job("
            "%s, %s, interval '1 hour', %s::text[])",
            (holder, credential, ["default"]),
        ).fetchone()
        assert claimed == ("running", 1)
        assert worker.execute(
            "SELECT public.heartbeat_worker_job(%s, %s, 1, interval '1 hour')",
            (job_id, credential),
        ).fetchone() == (True,)


def test_worker_lease_uses_postgres_clock_at_each_function_invocation(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """A caller's older transaction timestamp cannot backdate claim or heartbeat leases."""
    holder, credential = "docker:invocation-clock", b"t" * 32
    _register(role_dsn, holder, credential)
    job_id = _seed_queued_job(pg_conn)

    with psycopg.connect(role_dsn("kdive_worker")) as worker:
        worker.execute("SELECT 1")  # establish a transaction timestamp before the boundary call
        claim_reference = worker.execute("SELECT clock_timestamp()").fetchone()
        assert claim_reference is not None
        claimed = worker.execute(
            "SELECT heartbeat_at FROM public.claim_worker_job("
            "%s, %s, interval '5 minutes', %s::text[])",
            (holder, credential, ["default"]),
        ).fetchone()
        assert claimed is not None
        assert claimed[0] >= claim_reference[0]

        heartbeat_reference = worker.execute("SELECT clock_timestamp()").fetchone()
        assert heartbeat_reference is not None
        assert worker.execute(
            "SELECT public.heartbeat_worker_job(%s, %s, 1, interval '5 minutes')",
            (job_id, credential),
        ).fetchone() == (True,)
        heartbeat = worker.execute(
            "SELECT heartbeat_at FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()
        assert heartbeat is not None
        assert heartbeat[0] >= heartbeat_reference[0]


def test_worker_claim_lease_clock_starts_after_incarnation_lock_contention(
    pg_conn: psycopg.Connection, postgres_url: str, role_dsn: RoleDsns
) -> None:
    """A blocked claim receives its short lease only after the incarnation lock is acquired."""
    holder, credential = "docker:contended-claim", b"l" * 32
    _register(role_dsn, holder, credential)
    job_id = _seed_queued_job(pg_conn)
    connected = Event()
    claimant_pid: list[int] = []

    def claim() -> tuple[UUID, object, object] | None:
        with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker:
            claimant_pid.append(worker.info.backend_pid)
            connected.set()
            return worker.execute(
                "SELECT id, heartbeat_at, lease_expires_at FROM public.claim_worker_job("
                "%s, %s, interval '200 milliseconds', %s::text[])",
                (holder, credential, ["default"]),
            ).fetchone()

    with (
        psycopg.connect(postgres_url) as blocker,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        blocker.execute(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended('kdive:worker-incarnation:' || %s, 1803))",
            (holder,),
        )
        future = executor.submit(claim)
        try:
            assert connected.wait(timeout=DEFAULT_WAIT_TIMEOUT_S)
            wait_until_blocked_by(
                pg_conn,
                waiter_pid=claimant_pid[0],
                blocker_pid=blocker.info.backend_pid,
                future=future,
                expectation="claim did not block on the incarnation lock",
            )
            with pytest.raises(TimeoutError):
                future.result(timeout=0.4)
            held_until = pg_conn.execute("SELECT clock_timestamp()").fetchone()
            assert held_until is not None
        finally:
            blocker.commit()
        claimed = future.result(timeout=DEFAULT_WAIT_TIMEOUT_S)

    assert claimed is not None
    assert claimed[0] == job_id
    assert pg_conn.execute(
        "SELECT heartbeat_at >= %s, lease_expires_at > %s, "
        "lease_expires_at > heartbeat_at, "
        "lease_expires_at <= heartbeat_at + interval '200 milliseconds' "
        "FROM jobs WHERE id = %s",
        (held_until[0], held_until[0], job_id),
    ).fetchone() == (True, True, True, True)


def test_worker_heartbeat_lease_clock_starts_after_incarnation_and_job_lock_contention(
    pg_conn: psycopg.Connection, postgres_url: str, role_dsn: RoleDsns
) -> None:
    """A heartbeat starts its short lease after both ownership locks are acquired."""
    holder, credential = "docker:contended-heartbeat", b"n" * 32
    _register(role_dsn, holder, credential)
    _investigation, _generation, job_id = _seed_claim(pg_conn, holder=holder, attempt=1)
    connected = Event()
    heartbeat_pid: list[int] = []

    def heartbeat() -> bool:
        with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker:
            heartbeat_pid.append(worker.info.backend_pid)
            connected.set()
            row = worker.execute(
                "SELECT public.heartbeat_worker_job(%s, %s, 1, interval '200 milliseconds')",
                (job_id, credential),
            ).fetchone()
        assert row is not None
        return bool(row[0])

    with (
        psycopg.connect(postgres_url) as incarnation_blocker,
        psycopg.connect(postgres_url) as job_blocker,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        job_blocker.execute("SELECT 1 FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
        incarnation_blocker.execute(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended('kdive:worker-incarnation:' || %s, 1803))",
            (holder,),
        )
        future = executor.submit(heartbeat)
        try:
            assert connected.wait(timeout=DEFAULT_WAIT_TIMEOUT_S)
            wait_until_blocked_by(
                pg_conn,
                waiter_pid=heartbeat_pid[0],
                blocker_pid=incarnation_blocker.info.backend_pid,
                future=future,
                expectation="heartbeat did not block on the incarnation lock",
            )
            with pytest.raises(TimeoutError):
                future.result(timeout=0.4)
            incarnation_blocker.commit()

            wait_until_blocked_by(
                pg_conn,
                waiter_pid=heartbeat_pid[0],
                blocker_pid=job_blocker.info.backend_pid,
                future=future,
                expectation="heartbeat did not block on its exact running job row",
            )
            with pytest.raises(TimeoutError):
                future.result(timeout=0.4)
            held_until = pg_conn.execute("SELECT clock_timestamp()").fetchone()
            assert held_until is not None
        finally:
            incarnation_blocker.commit()
            job_blocker.commit()
        renewed = future.result(timeout=DEFAULT_WAIT_TIMEOUT_S)

    assert renewed is True
    assert pg_conn.execute(
        "SELECT heartbeat_at >= %s, lease_expires_at > %s, "
        "lease_expires_at > heartbeat_at, "
        "lease_expires_at <= heartbeat_at + interval '200 milliseconds' "
        "FROM jobs WHERE id = %s",
        (held_until[0], held_until[0], job_id),
    ).fetchone() == (True, True, True, True)


def test_worker_claimable_depth_preserves_outer_transaction_claim(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Worker telemetry counts through guarded aggregate access without rolling back its claim."""
    holder = "docker:claimable-depth"
    credential = SecretStr("claimable-depth-credential")
    credential_hash = hashlib.sha256(credential.get_secret_value().encode()).digest()
    _register(role_dsn, holder, credential_hash)
    job_id = _seed_queued_job(pg_conn)

    worker_login = role_dsn.logins["kdive_worker"]
    assert pg_conn.execute(
        "SELECT has_table_privilege(%s, 'public.capture_operations', 'SELECT')",
        (worker_login,),
    ).fetchone() == (False,)
    with (
        psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        worker.execute("SELECT count(*) FROM public.capture_operations")

    async def exercise() -> None:
        pool = AsyncConnectionPool(role_dsn("kdive_worker"), min_size=1, max_size=1, open=False)
        await pool.open()
        try:
            async with pool.connection() as conn:
                assert await queue.is_queue_paused(conn) is False
                claimed = await queue.dequeue(
                    conn,
                    holder,
                    incarnation_credential=credential,
                    accepted_lanes=("default",),
                )
                assert claimed is not None
                assert claimed.id == job_id
                assert await queue.count_claimable(conn, accepted_lanes=("default",)) == 0
        finally:
            await pool.close()

    asyncio.run(exercise())
    assert pg_conn.execute(
        "SELECT state, attempt, worker_id FROM public.jobs WHERE id = %s", (job_id,)
    ).fetchone() == ("running", 1, holder)


def test_worker_claimable_depth_function_authority_is_exact(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Only workers execute the aggregate, and its runtime guard survives an accidental grant."""
    signature = "count_claimable_worker_jobs(text[])"
    attributes = pg_conn.execute(
        "SELECT prosecdef, proconfig FROM pg_proc WHERE oid = %s::regprocedure",
        (signature,),
    ).fetchone()
    assert attributes == (True, ['search_path=""'])
    for role, login in role_dsn.logins.items():
        privilege = pg_conn.execute(
            "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
            (login, signature),
        ).fetchone()
        assert privilege == (role == "kdive_worker",), role

    with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker:
        for invalid_lanes in ([], [""], [None]):
            with pytest.raises(
                psycopg.errors.InvalidParameterValue,
                match="claimable-depth lanes are invalid",
            ):
                worker.execute(
                    "SELECT public.count_claimable_worker_jobs(%s::text[])",
                    (invalid_lanes,),
                )

    server_login = role_dsn.logins["kdive_server"]
    grant = SQL(
        "GRANT EXECUTE ON FUNCTION public.count_claimable_worker_jobs(text[]) TO {}"
    ).format(Identifier(server_login))
    revoke = SQL(
        "REVOKE EXECUTE ON FUNCTION public.count_claimable_worker_jobs(text[]) FROM {}"
    ).format(Identifier(server_login))
    pg_conn.execute(grant)
    try:
        with (
            psycopg.connect(role_dsn("kdive_server"), autocommit=True) as server,
            pytest.raises(
                psycopg.errors.InsufficientPrivilege,
                match="worker authority is required",
            ),
        ):
            server.execute(
                "SELECT public.count_claimable_worker_jobs(%s::text[])",
                (["default"],),
            )
    finally:
        pg_conn.execute(revoke)


def test_worker_job_function_execute_authority_is_exact(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Only the worker process role can invoke credential-bound job writes."""
    signatures = {
        "heartbeat_worker_job(uuid,bytea,integer,interval)",
        "complete_worker_job(uuid,bytea,integer,text)",
        "fail_worker_job(uuid,bytea,integer,text,jsonb,boolean)",
    }
    for signature in signatures:
        for role, login in role_dsn.logins.items():
            privilege = pg_conn.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                (login, signature),
            ).fetchone()
            assert privilege == (role == "kdive_worker",), (role, signature)


def test_protected_runtime_function_and_column_authority_is_exact(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Diagnostics, recovery, and GC expose no more protected evidence than each role needs."""
    allowed = {
        "list_investigation_build_uses(text[],timestamptz,uuid,integer)": {"kdive_server"},
        "recover_investigation_build_use(uuid,text[],text,text,text)": {
            "kdive_server",
            "kdive_reconciler",
        },
    }
    for signature, allowed_roles in allowed.items():
        for role, login in role_dsn.logins.items():
            assert pg_conn.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                (login, signature),
            ).fetchone() == (role in allowed_roles,), (role, signature)

    columns = {
        row[0]
        for row in pg_conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'investigation_build_uses'"
        ).fetchall()
    }
    for role, login in role_dsn.logins.items():
        for column in columns:
            expected = role == "kdive_reconciler" and column in {
                "investigation_id",
                "generation",
            }
            assert pg_conn.execute(
                "SELECT has_column_privilege(%s, 'investigation_build_uses', %s, 'SELECT')",
                (login, column),
            ).fetchone() == (expected,), (role, column)

    with psycopg.connect(role_dsn("kdive_server"), autocommit=True) as server:
        for invalid_limit in (0, 101):
            with pytest.raises(psycopg.errors.InvalidParameterValue, match="between 1 and 100"):
                server.execute(
                    "SELECT * FROM public.list_investigation_build_uses("
                    "%s::text[], NULL, NULL, %s)",
                    (["project-a"], invalid_limit),
                )
        with pytest.raises(psycopg.errors.InvalidParameterValue, match="boundary is incomplete"):
            server.execute(
                "SELECT * FROM public.list_investigation_build_uses(%s::text[], now(), NULL, 100)",
                (["project-a"],),
            )


def test_migration_upgrade_resets_guarded_function_matrix(
    pg_conn: psycopg.Connection, residual_privilege_role_dsn: RoleDsns
) -> None:
    """Default EXECUTE residue is removed before the intended guarded grants are restored."""
    allowed = {
        "register_worker_incarnation(text,text,jsonb,bytea,integer)": {"kdive_lifecycle_witness"},
        "authenticate_worker_incarnation(bytea)": {"kdive_worker"},
        "terminate_worker_incarnation(text,text,jsonb,text)": {"kdive_lifecycle_witness"},
        "acquire_investigation_build_use(uuid,uuid,uuid,uuid,integer,bytea)": {"kdive_worker"},
        "release_investigation_build_use(uuid,bytea)": {"kdive_worker"},
        "list_investigation_build_uses(text[],timestamptz,uuid,integer)": {"kdive_server"},
        "recover_investigation_build_use(uuid,text[],text,text,text)": {
            "kdive_server",
            "kdive_reconciler",
        },
        "claim_worker_job(text,bytea,interval,text[])": {"kdive_worker"},
        "count_claimable_worker_jobs(text[])": {"kdive_worker"},
        "register_kubernetes_worker_incarnation(text,jsonb,bytea,bytea,integer)": {
            "kdive_lifecycle_witness"
        },
        "create_capture_operation(bytea,uuid,integer,text,uuid,uuid,text,text)": {"kdive_worker"},
        "record_capture_operation_identity(bytea,uuid,text,text,integer,bigint)": {"kdive_worker"},
        "mark_capture_operation_running(bytea,uuid)": {"kdive_worker"},
        "request_capture_operation_cancel(bytea,uuid)": {"kdive_worker"},
        "acknowledge_capture_operation_exit(bytea,uuid,boolean,jsonb,text,integer)": {
            "kdive_worker"
        },
        "recover_capture_operation(bytea,uuid,boolean,jsonb,text,integer)": {"kdive_worker"},
        "capture_authenticated_worker(bytea)": set(),
        "capture_create_or_replay_operation("
        "text,uuid,integer,text,uuid,uuid,text,text,text)": set(),
        "capture_launch_abort_evidence_valid(capture_operations,jsonb)": set(),
        "capture_recovery_authorized(worker_incarnations,worker_incarnations)": set(),
        "capture_recovery_context(bytea,uuid)": set(),
        "capture_publication_operation(bytea,uuid,boolean,boolean)": set(),
        "begin_capture_publication(bytea,uuid,text)": {"kdive_worker"},
        "begin_cancel_capture_publication(bytea,uuid,text)": {"kdive_worker"},
        "record_capture_publication_version(bytea,uuid,text,text)": {"kdive_worker"},
        "record_capture_cleanup_version(bytea,uuid,text)": {"kdive_worker"},
        "commit_capture_published(bytea,uuid,jsonb,jsonb)": {"kdive_worker"},
        "commit_capture_discarded(bytea,uuid,text)": {"kdive_worker"},
        "record_capture_spool_disposed(bytea,uuid)": {"kdive_worker"},
        "read_kubernetes_credential_envelope(text,jsonb)": {"kdive_lifecycle_witness"},
        "acknowledge_kubernetes_credential_envelope(text,jsonb)": {"kdive_lifecycle_witness"},
    }
    for signature, allowed_roles in allowed.items():
        for canonical, login in residual_privilege_role_dsn.logins.items():
            privilege = pg_conn.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                (login, signature),
            ).fetchone()
            assert privilege == (canonical in allowed_roles,)

    credential = b"u" * 32
    with psycopg.connect(
        residual_privilege_role_dsn("kdive_lifecycle_witness"), autocommit=True
    ) as witness:
        witness.execute(
            "SELECT public.register_worker_incarnation(%s, 'docker', %s, %s, 4)",
            (
                "docker:upgrade-authority",
                Jsonb({"container_id": "a" * 64}),
                credential,
            ),
        )
    with psycopg.connect(residual_privilege_role_dsn("kdive_worker"), autocommit=True) as worker:
        authenticated = worker.execute(
            "SELECT incarnation FROM public.authenticate_worker_incarnation(%s)", (credential,)
        ).fetchone()
    assert authenticated == ("docker:upgrade-authority",)
    with psycopg.connect(
        residual_privilege_role_dsn("kdive_reconciler"), autocommit=True
    ) as reconciler:
        recovered = reconciler.execute(
            "SELECT public.recover_investigation_build_use("
            "%s, ARRAY['project-a'], 'holder', 'actor', 'reason')",
            (uuid4(),),
        ).fetchone()
    assert recovered == (False,)


@pytest.mark.parametrize(
    "collision_kind",
    ["login", "membership"],
)
def test_preexisting_runtime_role_collision_fails_closed(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns, collision_kind: str
) -> None:
    """Migration refuses a matching global name with login or escalation capability."""
    assert role_dsn("kdive_worker")
    role_sql = (migrate.SCHEMA_DIR / "0104_worker_fence_roles.sql").read_bytes()
    escalation_role = f"kdive_escalation_{pg_conn.info.dbname[-12:]}"
    pg_conn.execute("BEGIN")
    try:
        if collision_kind == "login":
            pg_conn.execute("ALTER ROLE kdive_worker LOGIN")
        else:
            pg_conn.execute(SQL("CREATE ROLE {} NOLOGIN").format(Identifier(escalation_role)))
            pg_conn.execute(SQL("GRANT {} TO kdive_worker").format(Identifier(escalation_role)))
        with pytest.raises(psycopg.errors.RaiseException, match="incompatible attributes"):
            pg_conn.execute(role_sql)
    finally:
        pg_conn.execute("ROLLBACK")


def test_concurrent_exact_runtime_role_creation_is_idempotent(
    pg_conn: psycopg.Connection, postgres_url: str
) -> None:
    """A concurrent exact role winner is revalidated instead of failing migration."""
    migrate.apply_migrations(pg_conn)
    suffix = pg_conn.info.dbname[-10:].replace("-", "_")
    roles = {
        "kdive_server": f"kdive_race_server_{suffix}",
        "kdive_worker": f"kdive_race_worker_{suffix}",
        "kdive_reconciler": f"kdive_race_reconciler_{suffix}",
        "kdive_lifecycle_witness": f"kdive_race_witness_{suffix}",
    }
    role_sql = (migrate.SCHEMA_DIR / "0104_worker_fence_roles.sql").read_bytes()
    for canonical, isolated in roles.items():
        role_sql = role_sql.replace(canonical.encode(), isolated.encode())

    creator = psycopg.connect(postgres_url)
    contender = psycopg.connect(postgres_url, autocommit=True)
    first_role = roles["kdive_server"]
    started = Event()

    def apply_role_migration() -> None:
        started.set()
        contender.execute(role_sql)

    try:
        creator.execute(
            SQL(
                "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(Identifier(first_role))
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(apply_role_migration)
            assert started.wait(timeout=DEFAULT_WAIT_TIMEOUT_S)
            wait_until_blocked_by(
                pg_conn,
                waiter_pid=contender.info.backend_pid,
                blocker_pid=creator.info.backend_pid,
                future=future,
                expectation="role migration did not block on concurrent exact role creation",
            )
            with pytest.raises(TimeoutError):
                future.result(timeout=0.5)
            creator.commit()
            future.result(timeout=DEFAULT_WAIT_TIMEOUT_S)
    finally:
        creator.rollback()
        contender.close()
        creator.close()
        for role in roles.values():
            if pg_conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                pg_conn.execute(SQL("DROP OWNED BY {}").format(Identifier(role)))
                pg_conn.execute(SQL("DROP ROLE {}").format(Identifier(role)))


@pytest.mark.parametrize("guarded", [False, True], ids=["unguarded-control", "fixture-lock"])
def test_cluster_global_role_lock_closes_validation_to_drop_window(
    postgres_url: str, *, guarded: bool
) -> None:
    """The control races; the fixture lock excludes the same cross-database role drop."""
    suffix = uuid4().hex[:10]
    migration_url, migration_db = db_conftest._provision_worker_db(
        postgres_url, dbname=f"kdive_role_migration_{suffix}"
    )
    drop_url, drop_db = db_conftest._provision_worker_db(
        postgres_url, dbname=f"kdive_role_drop_{suffix}"
    )
    roles = {
        "kdive_server": f"kdive_drop_server_{suffix}",
        "kdive_worker": f"kdive_drop_worker_{suffix}",
        "kdive_reconciler": f"kdive_drop_reconciler_{suffix}",
        "kdive_lifecycle_witness": f"kdive_drop_witness_{suffix}",
    }
    role_sql = (migrate.SCHEMA_DIR / "0104_worker_fence_roles.sql").read_bytes()
    for canonical, isolated in roles.items():
        role_sql = role_sql.replace(canonical.encode(), isolated.encode())
    pause_key = uuid4().int % (2**63 - 1)
    role_sql = role_sql.replace(
        b"        EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', v_role);",
        (
            f"        PERFORM pg_advisory_xact_lock({pause_key});\n"
            "        EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', v_role);"
        ).encode(),
        1,
    )
    try:
        with db_conftest._cluster_global_role_lock(postgres_url):
            for url in (migration_url, drop_url):
                with psycopg.connect(url, autocommit=True) as conn:
                    migrate.apply_migrations(conn)
        for role in roles.values():
            with psycopg.connect(migration_url, autocommit=True) as conn:
                conn.execute(
                    SQL(
                        "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                    ).format(Identifier(role))
                )

        blocker = psycopg.connect(migration_url)
        contender = psycopg.connect(migration_url, autocommit=True)
        dropper = psycopg.connect(drop_url, autocommit=True)
        observer = psycopg.connect(
            db_conftest._server_url_without_db(postgres_url), autocommit=True
        )
        try:
            blocker.execute("SELECT pg_advisory_xact_lock(%s)", (pause_key,))
            with ThreadPoolExecutor(max_workers=2) as executor:
                drop_lock_pids: Queue[int] = Queue(maxsize=1)
                migration_future = executor.submit(
                    _apply_role_sql,
                    migration_url,
                    contender,
                    role_sql,
                    guarded=guarded,
                )
                wait_until_blocked_by(
                    observer,
                    waiter_pid=contender.info.backend_pid,
                    blocker_pid=blocker.info.backend_pid,
                    future=migration_future,
                    expectation="role migration did not reach the pre-grant pause",
                )
                drop_future = executor.submit(
                    _drop_role,
                    drop_url,
                    dropper,
                    roles["kdive_server"],
                    guarded=guarded,
                    lock_backend_pids=drop_lock_pids,
                )
                if guarded:
                    _wait_for_cluster_role_lock_waiter(
                        observer,
                        drop_future,
                        waiter_pid=drop_lock_pids.get(timeout=DEFAULT_WAIT_TIMEOUT_S),
                    )
                    blocker.commit()
                    migration_future.result(timeout=DEFAULT_WAIT_TIMEOUT_S)
                    with pytest.raises(psycopg.errors.DependentObjectsStillExist):
                        drop_future.result(timeout=DEFAULT_WAIT_TIMEOUT_S)
                else:
                    drop_future.result(timeout=DEFAULT_WAIT_TIMEOUT_S)
                    blocker.commit()
                    with pytest.raises(psycopg.errors.UndefinedObject):
                        migration_future.result(timeout=DEFAULT_WAIT_TIMEOUT_S)
        finally:
            blocker.rollback()
            observer.close()
            dropper.close()
            contender.close()
            blocker.close()
    finally:
        _drop_isolated_roles(postgres_url, migration_url, drop_url, roles.values())
        db_conftest._drop_worker_db(postgres_url, drop_db)
        db_conftest._drop_worker_db(postgres_url, migration_db)


def _drop_isolated_roles(
    postgres_url: str, migration_url: str, drop_url: str, roles: Iterable[str]
) -> None:
    with (
        db_conftest._cluster_global_role_lock(postgres_url),
        psycopg.connect(migration_url, autocommit=True) as conn,
    ):
        for role in roles:
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                for url in (migration_url, drop_url):
                    with psycopg.connect(url, autocommit=True) as owner:
                        owner.execute(SQL("DROP OWNED BY {}").format(Identifier(role)))
                conn.execute(SQL("DROP ROLE {}").format(Identifier(role)))


def _apply_role_sql(url: str, conn: psycopg.Connection, role_sql: bytes, *, guarded: bool) -> None:
    if guarded:
        with db_conftest._cluster_global_role_lock(url):
            conn.execute(role_sql)
    else:
        conn.execute(role_sql)


def _drop_role(
    url: str,
    conn: psycopg.Connection,
    role: str,
    *,
    guarded: bool,
    lock_backend_pids: Queue[int],
) -> None:
    if guarded:
        with db_conftest._cluster_global_role_lock(url, _on_connect=lock_backend_pids.put):
            conn.execute(SQL("DROP ROLE {}").format(Identifier(role)))
    else:
        conn.execute(SQL("DROP ROLE {}").format(Identifier(role)))


def _wait_for_cluster_role_lock_waiter(
    observer: psycopg.Connection, future: Future[Any], *, waiter_pid: int
) -> None:
    deadline = time.monotonic() + DEFAULT_WAIT_TIMEOUT_S
    while time.monotonic() < deadline:
        waiting = observer.execute(
            "SELECT 1 FROM pg_locks AS l WHERE l.pid = %s AND l.locktype = 'advisory' "
            "AND l.database = ("
            "SELECT oid FROM pg_database WHERE datname = current_database()) "
            "AND l.classid = %s AND l.objid = %s AND l.objsubid = 2 "
            "AND NOT l.granted LIMIT 1",
            (waiter_pid, migrate._LOCK_CLASS_MIGRATION, migrate._LOCK_OBJID),
        ).fetchone()
        if waiting is not None:
            return
        if future.done():
            future.result()
            raise AssertionError("role drop returned before waiting for the fixture lock")
        time.sleep(0.02)
    raise AssertionError("role drop did not wait for the cluster-global fixture lock")


def test_acquire_derives_exact_locked_job_claim(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """A supplied job and attempt are only accepted when the credential owns that claim."""
    holder_a, holder_b = "docker:holder-a", "docker:holder-b"
    credential_a = b"a" * 32
    _register(role_dsn, holder_a, credential_a)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder_b, attempt=2)

    use_id = uuid4()
    assert not _acquire(
        role_dsn,
        use_id,
        investigation_id,
        generation,
        job_id,
        2,
        credential_a,
    )
    assert pg_conn.execute(
        "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s", (use_id,)
    ).fetchone() == (0,)


def test_acquire_persists_attempt_and_lease_from_locked_claim(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """The durable use copies claim facts from the locked jobs row."""
    holder, credential = "docker:exact-holder", b"e" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder, attempt=3)
    lease = pg_conn.execute("SELECT lease_expires_at FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert lease is not None

    use_id = uuid4()
    assert _acquire(
        role_dsn,
        use_id,
        investigation_id,
        generation,
        job_id,
        3,
        credential,
    )
    row = pg_conn.execute(
        "SELECT job_id, attempt, holder_worker_id, lease_expires_at "
        "FROM investigation_build_uses WHERE use_id = %s",
        (use_id,),
    ).fetchone()
    assert row == (job_id, 3, holder, lease[0])


def test_acquire_refuses_generation_outside_claimed_run(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """A valid worker claim cannot pin a different Run's generation."""
    holder, credential = "docker:wrong-generation", b"g" * 32
    _register(role_dsn, holder, credential)
    _claimed_investigation, _claimed_generation, job_id = _seed_claim(pg_conn, holder=holder)
    other_investigation, other_generation, _other_job = _seed_claim(pg_conn, holder=holder)

    assert not _acquire(
        role_dsn,
        uuid4(),
        other_investigation,
        other_generation,
        job_id,
        1,
        credential,
    )


def test_acquire_refuses_claim_with_mismatched_authorizing_project(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """The job, Run, and Investigation must retain one authoritative project."""
    holder, credential = "docker:wrong-project", b"p" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder, project="project-a")
    pg_conn.execute(
        "UPDATE jobs SET authorizing = %s WHERE id = %s",
        (Jsonb({"principal": "principal", "project": "project-b"}), job_id),
    )

    assert not _acquire(
        role_dsn,
        uuid4(),
        investigation_id,
        generation,
        job_id,
        1,
        credential,
    )


def test_reclaiming_generation_serializes_before_acquisition(
    pg_conn: psycopg.Connection, postgres_url: str, role_dsn: RoleDsns
) -> None:
    """Acquisition waits for the generation lock and then refuses reclaiming state."""
    holder, credential = "docker:reclaim-race", b"q" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder)
    use_id = uuid4()
    connected = Event()
    worker_pid: list[int] = []

    def acquire() -> bool:
        with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker:
            worker_pid.append(worker.info.backend_pid)
            connected.set()
            row = worker.execute(
                "SELECT public.acquire_investigation_build_use(%s, %s, %s, %s, %s, %s)",
                (use_id, investigation_id, generation, job_id, 1, credential),
            ).fetchone()
        assert row is not None
        return bool(row[0])

    with (
        psycopg.connect(postgres_url) as reclaim,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        reclaim.execute(
            "UPDATE investigation_builds SET state = 'reclaiming' "
            "WHERE investigation_id = %s AND generation = %s",
            (investigation_id, generation),
        )
        future = executor.submit(acquire)
        assert connected.wait(timeout=DEFAULT_WAIT_TIMEOUT_S)
        wait_until_blocked_by(
            pg_conn,
            waiter_pid=worker_pid[0],
            blocker_pid=reclaim.info.backend_pid,
            future=future,
            expectation="acquisition did not block on the reclaiming generation row",
        )
        with pytest.raises(TimeoutError):
            future.result(timeout=0.5)
        reclaim.commit()
        assert future.result(timeout=DEFAULT_WAIT_TIMEOUT_S) is False

    assert pg_conn.execute(
        "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s", (use_id,)
    ).fetchone() == (0,)


def test_acquire_refuses_replaced_attempt(pg_conn: psycopg.Connection, role_dsn: RoleDsns) -> None:
    """A stale provider cannot pin bytes after its job claim has been replaced."""
    holder, replacement = "docker:stale", "docker:replacement"
    credential = b"s" * 32
    _register(role_dsn, holder, credential)
    _register(role_dsn, replacement, b"t" * 32)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder, attempt=1)
    pg_conn.execute(
        "UPDATE jobs SET worker_id = %s, attempt = 2 WHERE id = %s", (replacement, job_id)
    )

    assert not _acquire(
        role_dsn,
        uuid4(),
        investigation_id,
        generation,
        job_id,
        1,
        credential,
    )


def test_release_refuses_replaced_attempt(pg_conn: psycopg.Connection, role_dsn: RoleDsns) -> None:
    """A credential cannot release a use after the job advances to another attempt."""
    holder, credential = "docker:release-holder", b"r" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder, attempt=1)
    use_id = uuid4()
    pg_conn.execute(
        "INSERT INTO investigation_build_uses (use_id, investigation_id, generation, job_id, "
        "attempt, holder_worker_id, lease_expires_at) VALUES "
        "(%s, %s, %s, %s, 1, %s, now() + interval '5 minutes')",
        (use_id, investigation_id, generation, job_id, holder),
    )
    pg_conn.execute("UPDATE jobs SET attempt = 2 WHERE id = %s", (job_id,))

    with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker:
        released = worker.execute(
            "SELECT public.release_investigation_build_use(%s, %s)", (use_id, credential)
        ).fetchone()
    assert released == (False,)
    assert pg_conn.execute(
        "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s", (use_id,)
    ).fetchone() == (1,)


def test_termination_serializes_before_acquisition(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Acquisition blocks on termination's incarnation lock and then observes terminal state."""
    holder, credential = "docker:termination-race", b"t" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder, attempt=1)
    use_id = uuid4()
    started = Event()

    def acquire() -> bool:
        with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker:
            started.set()
            row = worker.execute(
                "SELECT public.acquire_investigation_build_use(%s, %s, %s, %s, %s, %s)",
                (use_id, investigation_id, generation, job_id, 1, credential),
            ).fetchone()
        assert row is not None
        return bool(row[0])

    with (
        psycopg.connect(role_dsn("kdive_lifecycle_witness")) as witness,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        assert witness.execute(
            "SELECT public.terminate_worker_incarnation(%s, 'docker', %s, 'killed')",
            (holder, Jsonb({"container_id": "a" * 64})),
        ).fetchone() == (True,)
        future = executor.submit(acquire)
        assert started.wait(timeout=DEFAULT_WAIT_TIMEOUT_S)
        with pytest.raises(TimeoutError):
            future.result(timeout=0.5)
        witness.commit()
        assert future.result(timeout=DEFAULT_WAIT_TIMEOUT_S) is False

    assert pg_conn.execute(
        "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s", (use_id,)
    ).fetchone() == (0,)


def test_recovery_joins_and_persists_authoritative_project(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """A holder mismatch cannot cross uses, and the database-derived project is permanent."""
    holder, credential = "docker:recover-holder", b"v" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(
        pg_conn, holder=holder, project="project-a", attempt=1
    )
    use_id = uuid4()
    assert _acquire(
        role_dsn,
        use_id,
        investigation_id,
        generation,
        job_id,
        1,
        credential,
    )
    with psycopg.connect(role_dsn("kdive_lifecycle_witness"), autocommit=True) as witness:
        assert witness.execute(
            "SELECT public.terminate_worker_incarnation(%s, 'docker', %s, 'killed')",
            (holder, Jsonb({"container_id": "a" * 64})),
        ).fetchone() == (True,)

    with psycopg.connect(role_dsn("kdive_reconciler"), autocommit=True) as reconciler:
        foreign_scope = reconciler.execute(
            "SELECT public.recover_investigation_build_use(%s, %s::text[], %s, %s, %s)",
            (use_id, ["project-b"], holder, "reconciler:test", "worker terminated"),
        ).fetchone()
        holder_mismatch = reconciler.execute(
            "SELECT public.recover_investigation_build_use(%s, %s::text[], %s, %s, %s)",
            (
                use_id,
                ["project-a"],
                "docker:other-holder",
                "reconciler:test",
                "worker terminated",
            ),
        ).fetchone()
        recovered = reconciler.execute(
            "SELECT public.recover_investigation_build_use(%s, %s::text[], %s, %s, %s)",
            (use_id, ["project-a"], holder, "reconciler:test", "worker terminated"),
        ).fetchone()

    assert foreign_scope == (False,)
    assert holder_mismatch == (False,)
    assert recovered == (True,)
    assert pg_conn.execute(
        "SELECT project, investigation_id, generation FROM investigation_build_use_recoveries "
        "WHERE use_id = %s",
        (use_id,),
    ).fetchone() == ("project-a", investigation_id, generation)


def test_incarnation_identity_bound_is_bytes_at_table_and_function(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """A 512-codepoint multibyte identity cannot exceed the 512-byte persistence cap."""
    oversized_identity = "é" * 512
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
            "fence_protocol, credential_hash) VALUES (%s, 'docker', '{}'::jsonb, 4, %s)",
            (oversized_identity, b"i" * 32),
        )

    with (
        psycopg.connect(role_dsn("kdive_lifecycle_witness"), autocommit=True) as witness,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        witness.execute(
            "SELECT public.register_worker_incarnation(%s, 'docker', '{}'::jsonb, %s, 4)",
            (oversized_identity, b"j" * 32),
        )


def test_authority_binding_bound_is_serialized_bytes_at_table_and_function(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Both persistence paths reject a JSON object beyond the serialized byte cap."""
    oversized_binding = {"payload": "x" * _BINDING_MAX_BYTES}
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
            "fence_protocol, credential_hash) VALUES ('docker:large-direct', 'docker', %s, 4, %s)",
            (Jsonb(oversized_binding), b"b" * 32),
        )

    with (
        psycopg.connect(role_dsn("kdive_lifecycle_witness"), autocommit=True) as witness,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        witness.execute(
            "SELECT public.register_worker_incarnation(%s, 'docker', %s, %s, 4)",
            ("docker:large-function", Jsonb(oversized_binding), b"c" * 32),
        )


def test_kubernetes_envelope_is_exact_uid_bound_and_durably_cleared(
    role_dsn: RoleDsns,
) -> None:
    """Only the lifecycle authority can read/ack a pending exact Pod envelope."""
    holder = "kubernetes:kdive:kdive-worker-0:uid-1"
    binding = {
        "namespace": "kdive",
        "name": "kdive-worker-0",
        "uid": "uid-1",
    }
    envelope = b"controller-key-encrypted-envelope"
    with psycopg.connect(role_dsn("kdive_lifecycle_witness"), autocommit=True) as witness:
        assert witness.execute(
            "SELECT public.register_kubernetes_worker_incarnation(%s, %s, %s, %s, 4)",
            (holder, Jsonb(binding), b"e" * 32, envelope),
        ).fetchone() == (True,)
        assert witness.execute(
            "SELECT public.register_kubernetes_worker_incarnation(%s, %s, %s, %s, 4)",
            (holder, Jsonb(binding), b"f" * 32, b"replacement-envelope"),
        ).fetchone() == (True,)
        assert witness.execute(
            "SELECT public.read_kubernetes_credential_envelope(%s, %s)",
            (holder, Jsonb(binding)),
        ).fetchone() == (envelope,)
        assert witness.execute(
            "SELECT public.acknowledge_kubernetes_credential_envelope(%s, %s)",
            (holder, Jsonb(binding)),
        ).fetchone() == (True,)
        assert witness.execute(
            "SELECT public.acknowledge_kubernetes_credential_envelope(%s, %s)",
            (holder, Jsonb(binding)),
        ).fetchone() == (True,)
        assert witness.execute(
            "SELECT public.read_kubernetes_credential_envelope(%s, %s)",
            (holder, Jsonb(binding)),
        ).fetchone() == (None,)

    with (
        psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        worker.execute(
            "SELECT public.read_kubernetes_credential_envelope(%s, %s)",
            (holder, Jsonb(binding)),
        )


def test_kubernetes_registration_requires_a_bounded_matching_uid_binding(
    role_dsn: RoleDsns,
) -> None:
    with (
        psycopg.connect(role_dsn("kdive_lifecycle_witness"), autocommit=True) as witness,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        witness.execute(
            "SELECT public.register_kubernetes_worker_incarnation(%s, %s, %s, %s, 4)",
            (
                "kubernetes:kdive:kdive-worker-0:uid-1",
                Jsonb({"namespace": "kdive", "name": "kdive-worker-0", "uid": "uid-2"}),
                b"e" * 32,
                b"controller-key-encrypted-envelope",
            ),
        )
