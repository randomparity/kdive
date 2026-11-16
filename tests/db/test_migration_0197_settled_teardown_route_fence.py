"""Real-Postgres proof that a settled System has no authority teardown route (#3598)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from kdive.db import migrate
from tests.db.external_boot_authority_support import _acknowledge, _RoleDsns
from tests.db.stranded_activation_support import _call, _record_both, _strand

_ROUTES = "SELECT activation_id FROM resolve_external_boot_system_teardown_dispatch_binding(%s)"


def test_migration_0197_is_registered() -> None:
    assert "0197" in [item.version for item in migrate.discover_migrations()]


def _routes(role_dsns: _RoleDsns, system_id: UUID) -> list[tuple[Any, ...]]:
    # The server role is the resolver's only grantee: the call an older server makes.
    with psycopg.connect(role_dsns("kdive_server"), autocommit=True) as conn:
        return conn.execute(_ROUTES, (system_id,)).fetchall()


def test_settled_system_with_a_retired_authority_row_has_no_route(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    case = stranded.case
    assert stranded.authority is not None
    with psycopg.connect(migrated_url) as conn:
        _acknowledge(conn, case, stranded.authority)
        conn.execute(
            "UPDATE external_boot_authorities SET state = 'retired', retired_at = now() "
            "WHERE id = %s",
            (stranded.authority.authority_id,),
        )
    assert _routes(authority_role_dsns, case.system_id) == [(case.activation_id,)]
    _record_both(authority_role_dsns, stranded)
    assert _routes(authority_role_dsns, case.system_id) == [(case.activation_id,)]

    settle = "SELECT settle_stranded_external_boot_activation(%s,%s,%s,%s,%s)"
    args = (case.activation_id, "admin", "session", "operator-cli", "stranded")
    assert _call(authority_role_dsns, "kdive_server", settle, args) == "applied"

    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT state FROM external_boot_authorities WHERE id = %s",
            (stranded.authority.authority_id,),
        ).fetchone()
    assert row == ("retired",)
    assert _routes(authority_role_dsns, case.system_id) == []


def test_resolver_keeps_its_definer_and_grants(migrated_url: str) -> None:
    signature = "public.resolve_external_boot_system_teardown_dispatch_binding(uuid)"
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT prosecdef, has_function_privilege('kdive_server', %s, 'EXECUTE'), "
            "has_function_privilege('kdive_worker', %s, 'EXECUTE') "
            "FROM pg_proc WHERE oid = %s::regprocedure",
            (signature, signature, signature),
        ).fetchone()
    assert row == (True, True, False)
