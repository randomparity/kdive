"""Real-Postgres proofs that the job row holds a refusal's sizes and nothing else (#3535)."""

from __future__ import annotations

import re

import psycopg
import pytest

from kdive.db import migrate
from tests.db.external_boot_authority_support import (
    _COMMIT_SIGNATURE,
    _apply_through,
    _RoleDsns,
)
from tests.db.test_migration_0170_repeated_provider_conflict import _attempt, _case, _fail

_SIZES = {"vmlinux_bytes": 3, "free_bytes": 1, "headroom_bytes": 2}


def _context(refusal: object) -> dict[str, object]:
    return {"phase": "provider-call", "install_refusal": refusal}


def test_0193_patch_targets_exist_once(pg_conn: psycopg.Connection) -> None:
    _apply_through(pg_conn, "0189")
    definition = pg_conn.execute(
        "SELECT pg_get_functiondef(%s::regprocedure)", (f"public.{_COMMIT_SIGNATURE}",)
    ).fetchone()
    assert definition is not None
    sql = next(m for m in migrate.discover_migrations() if m.version == "0193").sql
    targets = re.findall(r"v_old_\w+ constant text := \$old\$(.*?)\$old\$;", sql, re.S)
    assert len(targets) == 2
    for target in targets:
        assert definition[0].count(target) == 1, target


def test_0193_a_terminal_install_failure_keeps_the_sizes(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _case(migrated_url, "a")
    first, authority = _attempt(migrated_url, authority_role_dsns, case, 1)

    assert _fail(
        authority_role_dsns,
        first,
        authority,
        _context(_SIZES),
        category="install_failure",
        terminal=True,
    ) == ("applied", "failed")

    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT state, attempt, error_category, failure_context FROM jobs WHERE id = %s",
            (case.job_id,),
        ).fetchone() == ("failed", 1, "install_failure", _context(_SIZES))


@pytest.mark.parametrize(
    "refusal",
    [
        "text",
        [],
        None,
        {},
        {"vmlinux_bytes": 3, "free_bytes": 1},
        {**_SIZES, "directory": "/boot"},
        {"vmlinux_bytes": 3, "free_bytes": 1, "headroom_byte": 2},
        {**_SIZES, "free_bytes": "1"},
        {**_SIZES, "free_bytes": True},
        {**_SIZES, "free_bytes": 1.5},
        {**_SIZES, "free_bytes": -1},
        {**_SIZES, "vmlinux_bytes": 2**63},
        {**_SIZES, "free_bytes": 5},
    ],
    ids=[
        "string",
        "array",
        "null",
        "empty",
        "missing-key",
        "extra-key",
        "renamed-key",
        "string-size",
        "bool-size",
        "fractional",
        "negative",
        "past-63-bits",
        "not-a-shortfall",
    ],
)
def test_0193_a_malformed_refusal_is_refused(
    migrated_url: str, authority_role_dsns: _RoleDsns, refusal: object
) -> None:
    case = _case(migrated_url, "b")
    first, authority = _attempt(migrated_url, authority_role_dsns, case, 1)

    with pytest.raises(psycopg.errors.InvalidParameterValue):
        _fail(
            authority_role_dsns,
            first,
            authority,
            _context(refusal),
            category="install_failure",
        )


def test_0193_sizes_on_another_category_are_refused(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _case(migrated_url, "c")
    first, authority = _attempt(migrated_url, authority_role_dsns, case, 1)

    with pytest.raises(psycopg.errors.InvalidParameterValue):
        _fail(authority_role_dsns, first, authority, _context(_SIZES))
