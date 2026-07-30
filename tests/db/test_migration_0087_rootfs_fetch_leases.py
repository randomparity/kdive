"""Migration 0087 adds the uploaded-rootfs fetch lease table (ADR-0515, #1702).

The table is the durable evidence a reclaim reads to tell "this base's download is in flight" from
"this base is unreferenced", in the window before a staging partial exists. Its two load-bearing
properties are the ones tested here: a lease is per-**holder** so sibling fetchers do not release
each other's, and it carries a deadline so a killed fetcher's row cannot pin a base forever.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import psycopg

from kdive.db import migrate


def _apply_through(conn: psycopg.Connection, last_version: str) -> None:
    """Apply migrations up to and including ``last_version`` without the migration runner."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    text PRIMARY KEY,
            filename   text NOT NULL,
            checksum   text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    for m in migrate.discover_migrations():
        if m.version > last_version:
            break
        conn.execute(m.sql.encode())
        conn.execute(
            "INSERT INTO schema_migrations (version, filename, checksum) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (m.version, m.filename, m.checksum),
        )


def _seed_system(conn: psycopg.Connection) -> tuple[str, str]:
    """An investigation and a System bound to it, the two parents a lease references."""
    inv_id, resource_id, alloc_id, system_id = (str(uuid.uuid4()) for _ in range(4))
    conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'p', 'proj', 't', 'open')",
        (inv_id,),
    )
    conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'p', 'c', 'available', 'qemu:///system')",
        (resource_id,),
    )
    conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'active', 'p', 'proj')",
        (alloc_id, resource_id),
    )
    conn.execute(
        "INSERT INTO systems (id, allocation_id, investigation_id, state, provisioning_profile, "
        "principal, project) VALUES (%s, %s, %s, 'provisioning', '{}'::jsonb, 'p', 'proj')",
        (system_id, alloc_id, inv_id),
    )
    return inv_id, system_id


def _insert_lease(
    conn: psycopg.Connection, inv_id: str, system_id: str, token: str, ttl: timedelta
) -> str:
    lease_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO rootfs_fetch_leases (id, investigation_id, token, system_id, expires_at) "
        "VALUES (%s, %s, %s, %s, now() + %s)",
        (lease_id, inv_id, token, system_id, ttl),
    )
    return lease_id


def _live_lease_exists(conn: psycopg.Connection, inv_id: str, token: str) -> bool:
    """The gate's own predicate, run against the schema rather than through the helper."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM rootfs_fetch_leases "
        "WHERE investigation_id = %s AND token = %s AND expires_at > now())",
        (inv_id, token),
    ).fetchone()
    assert row is not None
    return bool(row[0])


def test_0087_creates_the_lease_table_and_its_pin_predicate(pg_conn: psycopg.Connection) -> None:
    """An unexpired lease answers the pin predicate; a different token is unaffected."""
    _apply_through(pg_conn, "0087")
    inv_id, system_id = _seed_system(pg_conn)

    _insert_lease(pg_conn, inv_id, system_id, "token-x", timedelta(hours=6))

    assert _live_lease_exists(pg_conn, inv_id, "token-x")
    # Scoped to the checksum being reclaimed: a sibling staging a different base in the same
    # investigation must not stall this one for the length of an unrelated multi-GiB download.
    assert not _live_lease_exists(pg_conn, inv_id, "token-y")


def test_0087_an_expired_lease_does_not_satisfy_the_pin_predicate(
    pg_conn: psycopg.Connection,
) -> None:
    """AC-8's property at the schema level: a deadline in the past pins nothing.

    This is the whole reason the row carries ``expires_at``. A fetcher killed by SIGKILL releases
    nothing and nothing else ever clears its row — ``failed`` is terminal and ``torn_down`` is the
    achieved post-state — so a bare existence test would pin the base forever on precisely the path
    that matters. The base of a leak up to the 50 GiB canonical cap, per investigation, uncollected.
    """
    _apply_through(pg_conn, "0087")
    inv_id, system_id = _seed_system(pg_conn)

    _insert_lease(pg_conn, inv_id, system_id, "token-x", timedelta(hours=-1))

    # The row is still there — nothing has reaped it — and it still does not pin.
    row = pg_conn.execute(
        "SELECT count(*) FROM rootfs_fetch_leases WHERE investigation_id = %s", (inv_id,)
    ).fetchone()
    assert row is not None and row[0] == 1
    assert not _live_lease_exists(pg_conn, inv_id, "token-x")


def test_0087_sibling_fetchers_hold_independent_leases(pg_conn: psycopg.Connection) -> None:
    """Two fetchers of the same base each hold their own row, so neither releases the other's.

    The collision a ``(investigation_id, token)`` primary key would have had: the first sibling to
    finish would delete the row and unpin a base the second is still downloading. ADR-0502's
    ``object_write_leases`` puts the holder in its PK for the same reason.
    """
    _apply_through(pg_conn, "0087")
    inv_id, system_id = _seed_system(pg_conn)

    first = _insert_lease(pg_conn, inv_id, system_id, "token-x", timedelta(hours=6))
    _insert_lease(pg_conn, inv_id, system_id, "token-x", timedelta(hours=6))

    pg_conn.execute("DELETE FROM rootfs_fetch_leases WHERE id = %s", (first,))

    assert _live_lease_exists(pg_conn, inv_id, "token-x"), "a sibling's release dropped the pin"


def test_0087_a_lease_does_not_outlive_its_investigation(pg_conn: psycopg.Connection) -> None:
    """ON DELETE CASCADE: a lease whose investigation is gone pins nothing provisionable."""
    _apply_through(pg_conn, "0087")
    inv_id, system_id = _seed_system(pg_conn)
    _insert_lease(pg_conn, inv_id, system_id, "token-x", timedelta(hours=6))

    pg_conn.execute("DELETE FROM systems WHERE id = %s", (system_id,))
    pg_conn.execute("DELETE FROM investigations WHERE id = %s", (inv_id,))

    row = pg_conn.execute("SELECT count(*) FROM rootfs_fetch_leases").fetchone()
    assert row is not None and row[0] == 0
