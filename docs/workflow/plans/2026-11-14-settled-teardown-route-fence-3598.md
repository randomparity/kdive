# Settled teardown route fence (#3598) — implementation plan

Spec: [settled teardown route fence](../specs/2026-11-14-settled-teardown-route-fence-3598-design.md).

Expected implementation size: 130–170 changed lines (S) — migration ~55, test ~70, four window
tests ~8, ADR amendment ~20, runbook ~5.

## Global Constraints

- Migration number is exactly `0197` (assigned). `0198` is reserved for #3596.
- Applied migrations are byte-immutable (`just schema-guard`); never edit 0167 or 0194.
- ADR-0816 is append-only (`just records`): add one `### Amendment` block after the #3551
  amendment and before `## Considered & rejected`; change no existing line.
- No Python change under `src/` (spec, Scope).

## Task 1 — migration and its proof

Files: create `src/kdive/db/schema/0197_settled_teardown_route_fence.sql`,
`tests/db/test_migration_0197_settled_teardown_route_fence.py`; modify
`tests/db/test_migrate.py` (three bare-version lists and the `migrations[-92:]` list → `-93`),
`tests/db/test_migration_0091_system_object_sweep_cursors.py` (`-88` → `-89`),
`tests/db/test_migration_0102_build_gc_cursors.py` (`-88` → `-89`),
`tests/db/test_migration_0115_capture_reap_state.py` (`-75` → `-76`); each list gains
`("0197", "0197_settled_teardown_route_fence.sql")` (bare lists gain `"0197"`) after `0196`.

Interfaces consumed (exist on main `004ef4e7d`): `tests.db.stranded_activation_support._strand`
(`(migrated_url, role_dsns, *, suffix="s", allocate=True) -> _Stranded`, fields `case`,
`observer_job`, `authority`); `tests.db.external_boot_authority_support._acknowledge(conn, case,
authority)`; the SQL functions `record_stranded_activation_authority_absence`,
`record_stranded_activation_worker_ownership` and `settle_stranded_external_boot_activation`
(0194), called inline as the 0194 test does, because the repo imports helpers only from
`*_support.py` modules.

Verification:
- Settled route fence — `focused-test`:
  `test_settled_system_with_a_retired_authority_row_has_no_route`. Red before the migration:
  final assertion sees `[(activation_id,)]`. Green:
  `just test-verbose tests/db/test_migration_0197_settled_teardown_route_fence.py`.
- Unsettled route unchanged — `focused-test`: the same test asserts the route before recording
  and after both records (`settled_at` null); the marker branch keeps its route in the existing
  `tests/integration/test_external_boot_unrouted_teardown.py` and
  `tests/db/test_exhausted_retained_teardown.py`, run with
  `just test-verbose tests/integration/test_external_boot_unrouted_teardown.py tests/db/test_exhausted_retained_teardown.py tests/db/test_migration_0194_stranded_activation_settlement.py`.
- Grants and definer — `focused-test`: `test_resolver_keeps_its_definer_and_grants`.
- Registration — `focused-test`: the four window tests, run with
  `just test-verbose tests/db/test_migrate.py tests/db/test_migration_0091_system_object_sweep_cursors.py tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0115_capture_reap_state.py`.

Steps:
1. Write the test file:

```python
"""Real-Postgres proof that a settled System has no authority teardown route (#3598)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from kdive.db import migrate
from tests.db.external_boot_authority_support import _acknowledge, _RoleDsns
from tests.db.stranded_activation_support import _DOMAIN, _strand, _Stranded

_ROUTES = "SELECT activation_id FROM resolve_external_boot_system_teardown_dispatch_binding(%s)"


def test_migration_0197_is_registered() -> None:
    assert "0197" in [item.version for item in migrate.discover_migrations()]


def _routes(role_dsns: _RoleDsns, system_id: UUID) -> list[tuple[Any, ...]]:
    # The server role is the resolver's only grantee: the call an older server makes.
    with psycopg.connect(role_dsns("kdive_server"), autocommit=True) as conn:
        return conn.execute(_ROUTES, (system_id,)).fetchall()


def _call(role_dsns: _RoleDsns, role: str, sql: str, args: tuple[Any, ...]) -> Any:
    with psycopg.connect(role_dsns(role), autocommit=True) as conn:
        row = conn.execute(sql, args).fetchone()
    assert row is not None
    return row[0]


def _record_both(role_dsns: _RoleDsns, stranded: _Stranded) -> None:
    case = stranded.case
    authority = _call(
        role_dsns,
        "kdive_provider_authority",
        "SELECT record_stranded_activation_authority_absence(%s,%s,%s,%s,%s,%s,%s)",
        (
            case.activation_id,
            case.authority_instance,
            _DOMAIN,
            None,
            None,
            "sha256:" + "e" * 64,
            [],
        ),
    )
    worker = _call(
        role_dsns,
        "kdive_worker",
        "SELECT record_stranded_activation_worker_ownership(%s,%s,%s,%s,%s)",
        (case.credential, stranded.observer_job, 1, case.activation_id, _DOMAIN),
    )
    assert (authority, worker) == ("applied", "applied")


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
```

2. Run the focused command; expect `test_migration_0197_is_registered` and the settled test to
   fail (no 0197; route still returned).
3. Write the migration: header comment (`-- 0197_settled_teardown_route_fence.sql — #3598,
   ADR-0816 amendment (2026-11-14).`, forward-only per ADR-0015, why), then the 0167
   `CREATE OR REPLACE FUNCTION` statement verbatim with the spec's `NOT EXISTS` predicate added
   as the last condition of the first branch's outer `WHERE newest.state <> 'torn_down'` and of
   the second branch's `WHERE newest.state = 'preparing'`. No `REVOKE`/`GRANT`.
4. Update the four window tests as listed in Files.
5. Run the three focused commands; expect all green. Commit
   `fix(db): refuse a settled System's authority teardown route`.

Rollback: forward-only; a revert PR removes 0197 only before any deployment applied it.

## Task 2 — record and runbook

Files: `docs/adr/0816-stranded-activation-settlement.md`,
`docs/operating/runbooks/stranded-activation-recovery.md`.

Verification:
- ADR amendment and runbook text — `task-test-not-applicable`: prose with no executable
  consumer; `just records` and `just docs-links` gate shape and links.

Steps:
1. Append `### Amendment (2026-11-14): the resolver refuses a settled System (#3598)` after the
   #3551 amendment. It says it qualifies that amendment's upgrade-order Consequence; states that
   0197 returns no route for a settled newest activation, so an older server refuses with
   `external_boot_teardown_authority_unresolved` instead of routing to the authority; that the
   operator still needs an upgraded server to tear the System down; that the reconciler's
   place in the order is unchanged.
2. Rewrite the runbook's **Upgrade order** paragraph to match.
3. Run `just records` and `just docs-links`; expect exit 0. Commit
   `docs(adr): record the database settled-route fence`.
