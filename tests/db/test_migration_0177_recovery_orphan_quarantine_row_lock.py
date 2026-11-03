"""Migration 0177 lets kdive_server row-lock quarantine rows without rewriting them (#3322)."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import errors, sql

from kdive.db import migrate

_TABLE = "external_boot_recovery_quarantine"


def test_0177_grants_the_server_update_on_created_at_only(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    updatable = {
        row[0]
        for row in pg_conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = %s AND has_column_privilege('kdive_server', "
            "format('public.%%I', table_name), column_name, 'UPDATE')",
            (_TABLE,),
        ).fetchall()
    }
    assert updatable == {"created_at"}


@pytest.mark.parametrize(
    "column",
    ["status", "ownership_digest", "observed_digest", "mutation_journal_digest"],
)
def test_0177_server_still_cannot_update_fenced_columns(
    pg_conn: psycopg.Connection, column: str
) -> None:
    migrate.apply_migrations(pg_conn)
    with pg_conn.transaction(force_rollback=True):
        pg_conn.execute("SET LOCAL ROLE kdive_server")
        with pytest.raises(errors.InsufficientPrivilege):
            pg_conn.execute(
                sql.SQL("UPDATE {} SET {} = {}").format(
                    sql.Identifier(_TABLE), sql.Identifier(column), sql.Identifier(column)
                )
            )
