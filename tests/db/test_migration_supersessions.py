"""Reviewed supersessions of applied migrations (ADR-0818, #3612)."""

from __future__ import annotations

import psycopg
import pytest

from kdive.db import migrate, supersessions
from kdive.db.supersessions import Supersession

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64


def _entry(version: str = "0112", old: str = _A, new: str = _B) -> Supersession:
    return Supersession(version, f"{version}_x.sql", old, new, "ADR-0818")


def test_admits_the_exact_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supersessions, "SUPERSESSIONS", (_entry(),))
    assert supersessions.admits("0112", _A, _B)


@pytest.mark.parametrize(
    ("version", "recorded", "current"),
    [("0113", _A, _B), ("0112", _C, _B), ("0112", _A, _C), ("0112", _B, _A)],
)
def test_admits_nothing_else(
    monkeypatch: pytest.MonkeyPatch, version: str, recorded: str, current: str
) -> None:
    monkeypatch.setattr(supersessions, "SUPERSESSIONS", (_entry(),))
    assert not supersessions.admits(version, recorded, current)


def _recorded_0112(conn: psycopg.Connection) -> str:
    row = conn.execute("SELECT checksum FROM schema_migrations WHERE version = '0112'").fetchone()
    assert row is not None
    return row[0]


def test_runner_accepts_an_exact_supersession(
    pg_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrate.apply_migrations(pg_conn)
    current = _recorded_0112(pg_conn)
    pg_conn.execute("UPDATE schema_migrations SET checksum = %s WHERE version = '0112'", (_A,))
    monkeypatch.setattr(supersessions, "SUPERSESSIONS", (_entry(old=_A, new=current),))

    assert migrate.apply_migrations(pg_conn) == []
    assert _recorded_0112(pg_conn) == _A  # history is never rewritten


def test_runner_rejects_a_supersession_whose_current_bytes_differ(
    pg_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrate.apply_migrations(pg_conn)
    pg_conn.execute("UPDATE schema_migrations SET checksum = %s WHERE version = '0112'", (_A,))
    monkeypatch.setattr(supersessions, "SUPERSESSIONS", (_entry(old=_A, new=_C),))

    with pytest.raises(migrate.MigrationError, match="checksum changed"):
        migrate.apply_migrations(pg_conn)
