"""Real-Postgres contracts for the stranded observation's migration (#3574, ADR-0816)."""

from __future__ import annotations

from typing import Any, LiteralString, cast
from uuid import uuid4

import psycopg
import pytest

from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.stranded_activation_support import (
    _ALL_NAMED_OBJECTS,
    _DOMAIN,
    _SET_ABANDONED,
    _SET_ALL_NAMED_OBJECTS,
    _mark_authority_owned,
    _strand,
)

_READ = "SELECT * FROM read_stranded_activation_target(%s,%s)"


def _read(role_dsns: _RoleDsns, activation_id: object, instance: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(role_dsns("kdive_provider_authority"), autocommit=True) as conn:
        return conn.execute(_READ, (activation_id, instance)).fetchall()


def test_returns_the_stranded_target_without_a_head(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _strand(migrated_url, authority_role_dsns).case

    rows = _read(authority_role_dsns, case.activation_id, case.authority_instance)

    assert rows == [(case.system_id, case.run_id, _DOMAIN, [], None, None)]


def test_returns_the_journal_head(migrated_url: str, authority_role_dsns: _RoleDsns) -> None:
    case = _strand(migrated_url, authority_role_dsns).case
    digest = "sha256:" + "a" * 64
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO external_boot_authority_journal_heads "
            "(authority_instance, system_id, sequence, digest, authority_id, generation, phase, "
            "operation_identity, head_record) "
            "VALUES (%s, %s, 7, %s, %s, 1, 'terminal', 'op', '{}')",
            (case.authority_instance, case.system_id, digest, uuid4()),
        )

    rows = _read(authority_role_dsns, case.activation_id, case.authority_instance)

    assert [row[4:] for row in rows] == [(7, digest)]


def test_lists_every_named_object(migrated_url: str, authority_role_dsns: _RoleDsns) -> None:
    case = _strand(migrated_url, authority_role_dsns).case
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            cast(LiteralString, _SET_ALL_NAMED_OBJECTS.format(activation=f"'{case.activation_id}'"))
        )

    rows = _read(authority_role_dsns, case.activation_id, case.authority_instance)

    assert tuple(rows[0][3]) == _ALL_NAMED_OBJECTS


def test_no_row_for_a_missing_activation(authority_role_dsns: _RoleDsns) -> None:
    assert _read(authority_role_dsns, uuid4(), "any") == []


def test_no_row_for_a_foreign_instance(migrated_url: str, authority_role_dsns: _RoleDsns) -> None:
    case = _strand(migrated_url, authority_role_dsns).case

    assert _read(authority_role_dsns, case.activation_id, "another-authority") == []


@pytest.mark.parametrize(
    "statement", [_SET_ABANDONED, "UPDATE systems SET domain_name = NULL WHERE id = {system}"]
)
def test_no_row_outside_the_shape(
    migrated_url: str, authority_role_dsns: _RoleDsns, statement: str
) -> None:
    case = _strand(migrated_url, authority_role_dsns).case
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            cast(
                LiteralString,
                statement.format(
                    activation=f"'{case.activation_id}'", system=f"'{case.system_id}'"
                ),
            )
        )

    assert _read(authority_role_dsns, case.activation_id, case.authority_instance) == []


def test_no_row_for_an_authority_owned_system(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _strand(migrated_url, authority_role_dsns).case
    _mark_authority_owned(migrated_url, case)

    assert _read(authority_role_dsns, case.activation_id, case.authority_instance) == []


@pytest.mark.parametrize("role", ["kdive_server", "kdive_worker", "kdive_reconciler"])
def test_other_roles_cannot_read(role: str, authority_role_dsns: _RoleDsns) -> None:
    with (
        psycopg.connect(authority_role_dsns(role), autocommit=True) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(_READ, (uuid4(), "any"))


def test_job_kind_is_accepted(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'jobs_kind_check'"
        ).fetchone()

    assert row is not None
    assert "observe_stranded_activation" in row[0]
