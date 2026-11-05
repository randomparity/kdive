"""Real-Postgres contracts for the immutable authority System SSH forward port (#3546)."""

from __future__ import annotations

import psycopg
import pytest

from kdive.db import migrate
from tests.db.authority_system_support import _seed_direct_authority_case

_PORT = 41234


def _seed(conn: psycopg.Connection) -> object:
    case = _seed_direct_authority_case(conn, operation="provision", ownership_state="provisioning")
    return case["system_id"]


def _set_port(conn: psycopg.Connection, system_id: object, port: int | None) -> None:
    conn.execute(
        "UPDATE authority_system_ownership SET ssh_forward_port=%s WHERE system_id=%s",
        (port, system_id),
    )


def _port(conn: psycopg.Connection, system_id: object) -> object:
    row = conn.execute(
        "SELECT ssh_forward_port FROM authority_system_ownership WHERE system_id=%s",
        (system_id,),
    ).fetchone()
    assert row is not None
    return row[0]


def test_0191_is_registered_once() -> None:
    assert [m.version for m in migrate.discover_migrations()].count("0191") == 1


@pytest.mark.parametrize("rewrite", [_PORT + 1, None])
def test_0191_a_set_port_cannot_change(migrated_url: str, rewrite: int | None) -> None:
    with psycopg.connect(migrated_url) as conn:
        system_id = _seed(conn)
        _set_port(conn, system_id, _PORT)
        with pytest.raises(psycopg.errors.RaiseException, match="SSH forward port is immutable"):
            _set_port(conn, system_id, rewrite)


def test_0191_the_null_port_can_be_set(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        system_id = _seed(conn)
        _set_port(conn, system_id, _PORT)
        assert _port(conn, system_id) == _PORT


def test_0191_a_same_value_rewrite_is_allowed(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        system_id = _seed(conn)
        _set_port(conn, system_id, _PORT)
        _set_port(conn, system_id, _PORT)
        assert _port(conn, system_id) == _PORT


def test_0191_a_state_update_keeps_a_set_port(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        system_id = _seed(conn)
        _set_port(conn, system_id, _PORT)
        conn.execute(
            "UPDATE authority_system_ownership SET state='ready' WHERE system_id=%s",
            (system_id,),
        )
        assert _port(conn, system_id) == _PORT
