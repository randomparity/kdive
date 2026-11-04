"""Real-Postgres contracts for the authority System SSH forward (#3536, ADR-0811)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest

from kdive.db import migrate
from tests.db.authority_system_support import (
    _FINALIZE,
    _seed_direct_authority_case,
    _seed_direct_terminal_attempt,
)
from tests.db.external_boot_authority_support import _RoleDsns

_BINDING = (
    "SELECT ownership_state, ssh_forward_port FROM resolve_authority_system_control_binding(%s)"
)


def test_0189_is_registered_once() -> None:
    assert [m.version for m in migrate.discover_migrations()].count("0189") == 1


def _ready_case(
    migrated_url: str, fields: dict[str, Any] | None, *, own_system: bool = True
) -> tuple[dict[str, object], Any, Any, bytes, str]:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_direct_authority_case(
            conn, operation="provision", ownership_state="provisioning", attempt=1, max_attempts=3
        )
        receipt_fields = None
        if fields is not None:
            owner = str(case["system_id"]) if own_system else str(uuid4())
            receipt_fields = {"system_id": owner, **fields}
        authority_id, _, receipt, head, _ = _seed_direct_terminal_attempt(
            conn, case, disposition="provision-ready", receipt_fields=receipt_fields
        )
        conn.commit()
    return case, authority_id, None, receipt, head


def _finalize(migrated_url: str, role_dsns: _RoleDsns, fields: dict[str, Any] | None, **kw: Any):
    case, authority_id, _, receipt, head = _ready_case(migrated_url, fields, **kw)
    with psycopg.connect(role_dsns("kdive_worker")) as worker:
        row = worker.execute(
            _FINALIZE,
            (case["credential"], case["job_id"], 1, authority_id, 1, head, receipt),
        ).fetchone()
        worker.commit()
    assert row is not None
    return case, row


def _binding(role_dsns: _RoleDsns, case: dict[str, object]) -> tuple[object, ...]:
    with psycopg.connect(role_dsns("kdive_server")) as server:
        row = server.execute(_BINDING, (case["system_id"],)).fetchone()
    assert row is not None
    return row


def test_0189_finalizer_copies_the_reported_port_and_the_server_binding_returns_it(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    case, row = _finalize(migrated_url, role_dsns, {"ssh_forward_port": 41234})
    assert row[0] == "applied"
    assert _binding(role_dsns, case) == ("ready", 41234)


@pytest.mark.parametrize(
    "fields",
    [
        None,
        {},
        {"ssh_forward_port": 22},
        {"ssh_forward_port": 65536},
        {"ssh_forward_port": "41234"},
        {"ssh_forward_port": 41234.5},
        {"ssh_forward_port": True},
        {"ssh_forward_port": None},
    ],
)
def test_0189_absent_or_malformed_port_leaves_the_binding_without_one(
    migrated_url: str, request: pytest.FixtureRequest, fields: dict[str, Any] | None
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    case, row = _finalize(migrated_url, role_dsns, fields)
    assert row[0] == "applied"
    assert _binding(role_dsns, case) == ("ready", None)


def test_0189_a_receipt_naming_another_system_yields_no_port(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    case, _ = _finalize(migrated_url, role_dsns, {"ssh_forward_port": 41234}, own_system=False)
    assert _binding(role_dsns, case) == ("ready", None)


def test_0189_reconciler_repair_copies_the_port_too(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    case, _, _, _, _ = _ready_case(migrated_url, {"ssh_forward_port": 40001})
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (case["job_id"],),
        )
        conn.commit()
    with psycopg.connect(role_dsns("kdive_reconciler")) as reconciler:
        assert reconciler.execute(
            "SELECT repair_terminal_authority_system_attempts(10)"
        ).fetchone() == (1,)
        reconciler.commit()
    assert _binding(role_dsns, case) == ("ready", 40001)


def test_0189_binding_hides_the_port_outside_ready_and_activated(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    case, _ = _finalize(migrated_url, role_dsns, {"ssh_forward_port": 41234})
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE authority_system_ownership SET state='teardown-requested' WHERE system_id=%s",
            (case["system_id"],),
        )
        conn.commit()
    assert _binding(role_dsns, case) == ("teardown-requested", None)


def test_0189_the_column_refuses_an_out_of_range_port(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_direct_authority_case(
            conn, operation="provision", ownership_state="provisioning"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE authority_system_ownership SET ssh_forward_port=80 WHERE system_id=%s",
                (case["system_id"],),
            )


def test_0189_the_helper_is_not_callable_by_the_server_role(
    request: pytest.FixtureRequest, migrated_url: str
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    with (
        psycopg.connect(role_dsns("kdive_server")) as server,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        server.execute("SELECT authority_system_receipt_ssh_forward_port(%s,%s)", (b"{}", uuid4()))
