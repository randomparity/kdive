"""The uploaded-rootfs fetch lease helpers (ADR-0515, #1702).

The database behaviour of the table is covered against real Postgres in
``tests/db/test_migration_0087_rootfs_fetch_leases.py``, and the gate end-to-end in
``tests/jobs/handlers/test_rootfs_reclaim.py``. What this file pins is the module's own contract:
the TTL and the derivation it is supposed to follow, and the two degrade paths that must not turn a
database fault into a failed provision.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from kdive.providers.shared import rootfs_fetch_leases
from kdive.providers.shared.rootfs_fetch_leases import (
    ROOTFS_FETCH_LEASE_TTL,
    acquire_fetch_lease,
    release_fetch_lease,
)


class _RecordingConn:
    """A sync connection that records statements, optionally faulting on the lease ones."""

    def __init__(self, *, fault: bool = False, autocommit: bool = True) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.fault = fault
        self.autocommit = autocommit

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        if self.fault:
            raise psycopg.OperationalError("the connection pool is gone")
        self.statements.append((sql, params))


def test_the_ttl_follows_its_stated_derivation() -> None:
    # The TTL is the residual leak window this design accepts, so it is derived rather than picked
    # and the derivation is asserted rather than left in a comment to rot. Changing the constant
    # without changing the cap or the floor rate it comes from reddens here, which is the point:
    # shortening it trades the leak window against expiring under a live fetcher, and that second
    # failure is silent.
    cap = rootfs_fetch_leases._CANONICAL_BASE_CAP_BYTES
    floor = rootfs_fetch_leases._FLOOR_STAGING_THROUGHPUT_BYTES_S
    one_transfer = timedelta(seconds=cap / floor)

    assert cap == 50 * 1024**3  # the KDIVE_MAX_UPLOAD_BYTES default
    assert one_transfer == timedelta(seconds=10240)
    # The lease is taken before the session lock, so it covers a sibling's full transfer and then
    # this fetcher's own; the constant must not be below that, or it expires under a live download.
    assert 2 * one_transfer <= ROOTFS_FETCH_LEASE_TTL
    # ...and it is that figure rounded up, not an arbitrarily larger one: every extra hour is an
    # extra hour a killed fetcher's base stays on disk.
    assert timedelta(hours=6) == ROOTFS_FETCH_LEASE_TTL


def test_acquire_records_the_lease_and_returns_its_id() -> None:
    conn = _RecordingConn()
    inv, system_id = uuid4(), uuid4()

    lease_id = acquire_fetch_lease(conn, inv, "token-x", system_id=system_id)  # ty: ignore[invalid-argument-type]

    assert lease_id is not None
    sql, params = conn.statements[0]
    assert "INSERT INTO rootfs_fetch_leases" in sql
    assert params == (lease_id, inv, "token-x", system_id, ROOTFS_FETCH_LEASE_TTL)
    # The deadline is computed by Postgres from its own now(), never by the worker: this tree's
    # now() is session-TZ rather than UTC, and a Python-side deadline against a drifting worker
    # clock would expire a live lease early on exactly the hosts where that drift is worst.
    assert "now() + %s" in sql


def test_an_acquire_fault_degrades_to_no_lease_rather_than_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The pin is advisory. Failing here would turn a transient database blip into a total
    # uploaded-rootfs provisioning outage, where degrading only reverts the reclaim to its
    # pre-ADR-0515 reach. None is the caller's signal that there is nothing to release.
    conn = _RecordingConn(fault=True)

    with caplog.at_level("WARNING"):
        assert acquire_fetch_lease(conn, uuid4(), "token-x", system_id=uuid4()) is None  # ty: ignore[invalid-argument-type]

    # Reported, because the log line is the only evidence the degrade fired — the provision
    # succeeds either way.
    assert "staging unleased" in caplog.text


def test_a_release_fault_is_reported_and_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    # release_fetch_lease runs from the fetch's `finally`. Raising there would REPLACE the in-flight
    # exception, demoting an actionable CategorizedError — a checksum mismatch, a non-qcow2 upload —
    # to __context__ behind a Postgres message. That is the defect _release_fetch_lock documents for
    # the advisory lock one call away, and it arrives here for the same reason: on the crash shape
    # the TTL exists to bound, the connection is frequently already gone by the time this runs.
    conn = _RecordingConn(fault=True)

    with caplog.at_level("WARNING"):
        release_fetch_lease(conn, uuid4())  # ty: ignore[invalid-argument-type]

    # The remedy is named: the row is not orphaned forever, it expires.
    assert "until it expires" in caplog.text


def test_a_non_autocommit_connection_records_no_lease(caplog: pytest.LogCaptureFixture) -> None:
    # The one way ADR-0515 could fail silently and totally. Inside a transaction the row is
    # invisible to the reclaim's separate connection until commit — on the production path, after
    # the download the lease exists to protect has already finished. The fetch would still succeed
    # and nothing would raise, so the mechanism would be a no-op that every test still passed.
    # Recording a lease that pins nothing is strictly worse than recording none: it would read as
    # protection in the table an operator inspects.
    conn = _RecordingConn(autocommit=False)

    with caplog.at_level("WARNING"):
        assert acquire_fetch_lease(conn, uuid4(), "token-x", system_id=uuid4()) is None  # ty: ignore[invalid-argument-type]

    assert conn.statements == []
    assert "not in autocommit" in caplog.text
