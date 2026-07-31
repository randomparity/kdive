"""Migration 0090 fences the uploaded-rootfs fetch lease on its holding job (ADR-0522, #1740).

0087 gave the lease a 6-hour ``expires_at`` because the provision seam carried no job identity, so
there was nothing to fence on; #1740 threaded the provision job's id through that seam. What this
file pins is the swap at the schema level: the pin predicate is now the holding job's own liveness,
a lease cannot be recorded without a holder, and the row still does not outlive anything it names.

The sibling-independence and investigation-cascade properties 0087 established are re-asserted here
against the new shape rather than left to the retired 0087 test, because both are load-bearing and
both now travel through a column that did not exist when they were first written.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db import migrate


def _apply_through(conn: psycopg.Connection, last_version: str) -> None:
    """Apply migrations up to and including ``last_version`` without the migration runner.

    Resumable: already-recorded versions are skipped, so a test can stop at ``0087``, write a row
    the way a pre-fence worker did, and then apply ``0090`` over it. Re-executing ``0001`` would
    otherwise fail on the tables it already created.
    """
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
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for m in migrate.discover_migrations():
        if m.version > last_version:
            break
        if m.version in applied:
            continue
        conn.execute(m.sql.encode())
        conn.execute(
            "INSERT INTO schema_migrations (version, filename, checksum) VALUES (%s, %s, %s)",
            (m.version, m.filename, m.checksum),
        )


def _seed_system(conn: psycopg.Connection) -> tuple[str, str]:
    """An investigation and a System bound to it, two of the three parents a lease references."""
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


def _seed_job(conn: psycopg.Connection, *, state: str, lease: timedelta) -> str:
    """A ``jobs`` row in a named liveness shape.

    ``lease`` offsets ``lease_expires_at`` from Postgres ``now()``: a positive one is a claim the
    worker is still heartbeating, a negative one is the claim a killed worker stopped renewing. The
    offset is applied by Postgres, not Python, for the same reason the gate's predicate is — this
    tree's ``now()`` is session-TZ rather than UTC.
    """
    job_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO jobs (id, kind, state, max_attempts, authorizing, dedup_key, "
        "lease_expires_at) VALUES (%s, 'provision', %s, 3, %s, %s, now() + %s)",
        (job_id, state, Jsonb({"principal": "p", "project": "proj"}), f"dedup-{job_id}", lease),
    )
    return job_id


def _insert_lease(
    conn: psycopg.Connection, inv_id: str, system_id: str, token: str, job_id: str
) -> str:
    lease_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO rootfs_fetch_leases (id, investigation_id, token, system_id, job_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (lease_id, inv_id, token, system_id, job_id),
    )
    return lease_id


def _live_lease_exists(conn: psycopg.Connection, inv_id: str, token: str) -> bool:
    """The gate's own predicate, run against the schema rather than through the helper."""
    row = conn.execute(
        """SELECT EXISTS (SELECT 1 FROM rootfs_fetch_leases l
                          WHERE l.investigation_id = %s AND l.token = %s
                            AND EXISTS (SELECT 1 FROM jobs j
                                        WHERE j.id = l.job_id
                                          AND j.state = 'running'
                                          AND j.lease_expires_at > now()))""",
        (inv_id, token),
    ).fetchone()
    assert row is not None
    return bool(row[0])


def test_0090_a_lease_held_by_a_live_job_pins_its_base(pg_conn: psycopg.Connection) -> None:
    """A running job with an un-lapsed lease pins; a different token is unaffected."""
    _apply_through(pg_conn, "0090")
    inv_id, system_id = _seed_system(pg_conn)
    job_id = _seed_job(pg_conn, state="running", lease=timedelta(minutes=5))

    _insert_lease(pg_conn, inv_id, system_id, "token-x", job_id)

    assert _live_lease_exists(pg_conn, inv_id, "token-x")
    # Scoped to the checksum being reclaimed: a sibling staging a different base in the same
    # investigation must not stall this one for the length of an unrelated multi-GiB download.
    assert not _live_lease_exists(pg_conn, inv_id, "token-y")


@pytest.mark.parametrize(
    ("state", "lease", "why"),
    [
        (
            "running",
            timedelta(minutes=-1),
            "the worker was killed and stopped renewing its job lease",
        ),
        (
            "failed",
            timedelta(minutes=5),
            "the job reached a terminal state with time left on its lease",
        ),
        ("succeeded", timedelta(minutes=5), "the job finished without the fetch releasing its row"),
        ("queued", timedelta(minutes=5), "the job was requeued, so no worker holds this fetch"),
    ],
)
def test_0090_a_lease_whose_holder_is_not_a_live_claim_pins_nothing(
    pg_conn: psycopg.Connection, state: str, lease: timedelta, why: str
) -> None:
    """AC-8's property at the schema level, on every shape of a holder that is no longer running.

    This is what ``expires_at`` bought before 0090 and what the job fence buys now, without the
    derived constant. The first case is the one that matters most: a fetcher killed by ``SIGKILL``
    releases nothing, and nothing in the lease module ever clears its row — ``failed`` is terminal
    and ``torn_down`` is the achieved post-state — so a bare existence test would pin a base of up
    to the 50 GiB canonical cap forever, per investigation, uncollected.

    Where ADR-0515 had to wait out a worst-case transfer estimate, the pin now lapses on the
    job-lease interval the worker was already heartbeating.
    """
    _apply_through(pg_conn, "0090")
    inv_id, system_id = _seed_system(pg_conn)
    job_id = _seed_job(pg_conn, state=state, lease=lease)

    _insert_lease(pg_conn, inv_id, system_id, "token-x", job_id)

    # The row is still there — nothing has reaped it — and it still does not pin.
    row = pg_conn.execute(
        "SELECT count(*) FROM rootfs_fetch_leases WHERE investigation_id = %s", (inv_id,)
    ).fetchone()
    assert row is not None and row[0] == 1
    assert not _live_lease_exists(pg_conn, inv_id, "token-x"), why


def test_0090_a_lease_cannot_be_recorded_without_a_holder(pg_conn: psycopg.Connection) -> None:
    """``job_id`` is ``NOT NULL``: a holderless row is the unbounded pin the fence exists to stop.

    The column that replaced the deadline must not be optional. A row naming no job satisfies no
    liveness test and nothing ever clears it, so it would pin its base until an operator noticed —
    strictly worse than the 6 hours 0087 accepted. The database refuses it rather than trusting
    every writer to remember.
    """
    _apply_through(pg_conn, "0090")
    inv_id, system_id = _seed_system(pg_conn)

    with pytest.raises(psycopg.errors.NotNullViolation):
        pg_conn.execute(
            "INSERT INTO rootfs_fetch_leases (id, investigation_id, token, system_id) "
            "VALUES (%s, %s, 'token-x', %s)",
            (str(uuid.uuid4()), inv_id, system_id),
        )


def test_0090_the_lease_carries_no_second_deadline(pg_conn: psycopg.Connection) -> None:
    """``expires_at`` is gone, not merely unread — ADR-0522 deletes §4 rather than tuning it.

    Leaving the column would leave a writer able to set it and a reader able to believe it, which is
    two definitions of when this pin ends. The schema keeps one.
    """
    _apply_through(pg_conn, "0090")

    row = pg_conn.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'rootfs_fetch_leases' AND column_name = 'expires_at'"
    ).fetchone()
    assert row is not None and row[0] == 0


def test_0090_sibling_fetchers_hold_independent_leases(pg_conn: psycopg.Connection) -> None:
    """Two fetchers of the same base each hold their own row, so neither releases the other's.

    The collision a ``(investigation_id, token)`` primary key would have had: the first sibling to
    finish would delete the row and unpin a base the second is still downloading. Unchanged by the
    fence swap, and re-asserted here because the surviving pin's liveness now travels through
    ``job_id`` — two siblings under *different* provision jobs is the production shape.
    """
    _apply_through(pg_conn, "0090")
    inv_id, system_id = _seed_system(pg_conn)
    first_job = _seed_job(pg_conn, state="running", lease=timedelta(minutes=5))
    second_job = _seed_job(pg_conn, state="running", lease=timedelta(minutes=5))

    first = _insert_lease(pg_conn, inv_id, system_id, "token-x", first_job)
    _insert_lease(pg_conn, inv_id, system_id, "token-x", second_job)

    pg_conn.execute("DELETE FROM rootfs_fetch_leases WHERE id = %s", (first,))

    assert _live_lease_exists(pg_conn, inv_id, "token-x"), "a sibling's release dropped the pin"


def test_0090_a_lease_does_not_outlive_its_job(pg_conn: psycopg.Connection) -> None:
    """``ON DELETE CASCADE`` on ``job_id``: a lease with no ``jobs`` row protects nothing.

    The same rule ``object_write_leases`` takes (ADR-0502 / migration 0084), for the same
    relationship. It covers the job-retention sweep; the reap collects the far commoner case of a
    job that merely stopped running.
    """
    _apply_through(pg_conn, "0090")
    inv_id, system_id = _seed_system(pg_conn)
    job_id = _seed_job(pg_conn, state="running", lease=timedelta(minutes=5))
    _insert_lease(pg_conn, inv_id, system_id, "token-x", job_id)

    pg_conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))

    row = pg_conn.execute("SELECT count(*) FROM rootfs_fetch_leases").fetchone()
    assert row is not None and row[0] == 0


def test_0090_a_lease_does_not_outlive_its_investigation(pg_conn: psycopg.Connection) -> None:
    """The 0087 cascade, unchanged: a lease whose investigation is gone pins nothing."""
    _apply_through(pg_conn, "0090")
    inv_id, system_id = _seed_system(pg_conn)
    job_id = _seed_job(pg_conn, state="running", lease=timedelta(minutes=5))
    _insert_lease(pg_conn, inv_id, system_id, "token-x", job_id)

    pg_conn.execute("DELETE FROM systems WHERE id = %s", (system_id,))
    pg_conn.execute("DELETE FROM investigations WHERE id = %s", (inv_id,))

    row = pg_conn.execute("SELECT count(*) FROM rootfs_fetch_leases").fetchone()
    assert row is not None and row[0] == 0


def test_0090_drops_rows_that_predate_the_fence(pg_conn: psycopg.Connection) -> None:
    """A lease written before 0090 names no job, so the migration collects rather than keeps it.

    Not data loss: the rows are transient evidence about in-flight downloads, and one that cannot
    satisfy the new fence would be a permanent pin under a ``NOT NULL`` column it has nothing to
    put in. The bounded cost is stated in 0090's own comment — one reclaim pass may proceed as it
    did before ADR-0515 for a fetch straddling the upgrade, still covered by ADR-0495's flock probe
    once that fetch reaches its partial.
    """
    _apply_through(pg_conn, "0087")
    inv_id, system_id = _seed_system(pg_conn)
    pg_conn.execute(
        "INSERT INTO rootfs_fetch_leases (id, investigation_id, token, system_id, expires_at) "
        "VALUES (%s, %s, 'token-x', %s, now() + interval '6 hours')",
        (str(uuid.uuid4()), inv_id, system_id),
    )

    _apply_through(pg_conn, "0090")

    row = pg_conn.execute("SELECT count(*) FROM rootfs_fetch_leases").fetchone()
    assert row is not None and row[0] == 0
