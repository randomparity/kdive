"""Real-Postgres proofs that a repeated provider-conflict ends the external-boot job (#2901)."""

from __future__ import annotations

import re

import psycopg
import pytest

from kdive.db import migrate
from tests.db.external_boot_authority_support import (
    _COMMIT_SIGNATURE,
    _apply_through,
    _attempt,
    _AuthorityCase,
    _case,
    _fail,
    _RoleDsns,
)

_CONFLICT = {"phase": "commit", "authority_reason": "provider-conflict"}


def _migration_sql() -> str:
    return next(m for m in migrate.discover_migrations() if m.version == "0170").sql


def _fail_audit(migrated_url: str, case: _AuthorityCase) -> list[tuple[object, ...]]:
    with psycopg.connect(migrated_url) as conn:
        return conn.execute(
            "SELECT job_attempt, outcome, failure_context FROM external_boot_authority_audit "
            "WHERE job_id = %s AND outcome IN ('result_requeued', 'result_failed') "
            "ORDER BY job_attempt",
            (case.job_id,),
        ).fetchall()


def test_0170_patch_targets_exist_once(pg_conn: psycopg.Connection) -> None:
    _apply_through(pg_conn, "0169")
    definition = pg_conn.execute(
        "SELECT pg_get_functiondef(%s::regprocedure)", (f"public.{_COMMIT_SIGNATURE}",)
    ).fetchone()
    assert definition is not None
    targets = re.findall(r"v_old_\w+ constant text := \$old\$(.*?)\$old\$;", _migration_sql(), re.S)
    assert len(targets) == 5
    for target in targets:
        assert definition[0].count(target) == 1, target


def test_0170_second_identical_provider_conflict_is_terminal(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _case(migrated_url, "a")
    first, authority = _attempt(migrated_url, authority_role_dsns, case, 1)
    assert _fail(authority_role_dsns, first, authority, _CONFLICT) == ("applied", "queued")
    second, authority = _attempt(migrated_url, authority_role_dsns, case, 2)
    assert _fail(authority_role_dsns, second, authority, _CONFLICT) == ("applied", "failed")
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT state, attempt, error_category, failure_context FROM jobs WHERE id = %s",
            (case.job_id,),
        ).fetchone() == ("failed", 2, "infrastructure_failure", _CONFLICT)
    assert _fail_audit(migrated_url, case) == [
        (1, "result_requeued", _CONFLICT),
        (2, "result_failed", _CONFLICT),
    ]


@pytest.mark.parametrize("variant", ["no-reason", "phase-differs", "older-budget"])
def test_0170_non_identical_failures_still_requeue(
    migrated_url: str, authority_role_dsns: _RoleDsns, variant: str
) -> None:
    contexts: tuple[dict[str, str], dict[str, str]] = {
        "no-reason": ({"phase": "commit"}, {"phase": "commit"}),
        "phase-differs": ({**_CONFLICT, "phase": "provider-call"}, _CONFLICT),
        "older-budget": (_CONFLICT, _CONFLICT),
    }[variant]
    case = _case(migrated_url, variant[0])
    first, authority = _attempt(migrated_url, authority_role_dsns, case, 1)
    assert _fail(authority_role_dsns, first, authority, contexts[0]) == ("applied", "queued")
    if variant == "older-budget":
        with psycopg.connect(migrated_url) as seed:
            seed.execute(
                "UPDATE jobs SET created_at = clock_timestamp() + interval '1 second' "
                "WHERE id = %s",
                (case.job_id,),
            )
    second, authority = _attempt(migrated_url, authority_role_dsns, case, 2)
    assert _fail(authority_role_dsns, second, authority, contexts[1]) == ("applied", "queued")
    if variant == "no-reason":
        assert _fail_audit(migrated_url, case) == [
            (1, "result_requeued", None),
            (2, "result_requeued", None),
        ]


def test_0170_terminal_repeat_leaves_reservation_credit(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _case(migrated_url, "c")

    def credit() -> tuple[object, ...]:
        with psycopg.connect(migrated_url) as conn:
            reservation = conn.execute(
                "SELECT state, reserved_bytes FROM external_boot_reservations "
                "WHERE activation_id = %s",
                (case.activation_id,),
            ).fetchone()
            releases = conn.execute(
                "SELECT count(*) FROM external_boot_reservation_releases WHERE activation_id = %s",
                (case.activation_id,),
            ).fetchone()
        return (reservation, releases)

    assert credit() == (("ready", 4096), (0,))
    first, authority = _attempt(migrated_url, authority_role_dsns, case, 1)
    assert _fail(authority_role_dsns, first, authority, _CONFLICT) == ("applied", "queued")
    second, authority = _attempt(migrated_url, authority_role_dsns, case, 2)
    assert _fail(authority_role_dsns, second, authority, _CONFLICT) == ("applied", "failed")
    assert credit() == (("ready", 4096), (0,))


@pytest.mark.parametrize(
    ("reason", "category"),
    [("other", "infrastructure_failure"), ("provider-conflict", "boot_timeout")],
)
def test_0170_refuses_an_unknown_or_miscategorized_reason(
    migrated_url: str, authority_role_dsns: _RoleDsns, reason: str, category: str
) -> None:
    case = _case(migrated_url, "r")
    first, authority = _attempt(migrated_url, authority_role_dsns, case, 1)
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        _fail(
            authority_role_dsns,
            first,
            authority,
            {"phase": "commit", "authority_reason": reason},
            category,
        )
