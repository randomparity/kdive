"""The uploaded-rootfs fetch lease helpers (ADR-0515 as amended by ADR-0522, #1702/#1740).

The database behaviour of the table is covered against real Postgres in
``tests/db/test_migration_0090_rootfs_fetch_lease_job_fence.py``, and the gate end-to-end in
``tests/jobs/handlers/test_rootfs_reclaim.py``. What this file pins is the module's own contract:
that the pin is fenced on its holding job rather than on a deadline, and the three degrade paths
that must not turn a missing fence into a failed provision.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest

from kdive.artifacts.uploads.write_lease import LIVE_HOLDER_SQL
from kdive.providers.shared.staging import rootfs_fetch_leases
from kdive.providers.shared.staging.rootfs_fetch_leases import (
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


def test_the_pin_is_fenced_on_the_holding_jobs_liveness() -> None:
    """AC-8's property, at the statement the reclaim actually runs (ADR-0522).

    This is the guard the mutation proof targets: drop the ``LIVE_HOLDER_SQL`` conjunct from
    ``_PIN_SQL`` and this reddens, because the probe would then treat a dead fetcher's abandoned row
    as a pin — an unbounded pin on the one path that matters, which is the disk-exhaustion
    regression ``test_failed_referencer_with_overlay_gone_drains`` exists to catch. ADR-0515 §4
    spent a derived 6-hour deadline buying the same property; nothing here may reintroduce one.
    """
    assert LIVE_HOLDER_SQL in rootfs_fetch_leases._PIN_SQL
    # Both halves, spelled out, so a "simplification" to a bare state test (which a job whose worker
    # died still satisfies until the reaper runs) or to a bare deadline test reddens here.
    assert "j.state = 'running'" in rootfs_fetch_leases._PIN_SQL
    assert "j.lease_expires_at > now()" in rootfs_fetch_leases._PIN_SQL
    assert "j.id = l.job_id" in rootfs_fetch_leases._PIN_SQL
    # No second, independent deadline of the lease's own: that is the constant ADR-0522 removed, and
    # re-adding one would restore the leak window without buying anything the holder does not.
    assert "expires_at" not in rootfs_fetch_leases._ACQUIRE_SQL


def test_the_reap_is_never_looser_than_the_gate() -> None:
    # The pass that HONOURS a lease and the pass that COLLECTS one read the same predicate, so they
    # cannot disagree about which leases are live. A reap looser than the fence would delete a row
    # that is actively protecting a multi-GiB download — the failure the shared constant prevents.
    assert LIVE_HOLDER_SQL in rootfs_fetch_leases._REAP_SQL
    assert f"NOT {LIVE_HOLDER_SQL}" in rootfs_fetch_leases._REAP_SQL


def test_acquire_records_the_lease_and_returns_its_id() -> None:
    conn = _RecordingConn()
    inv, system_id, job_id = uuid4(), uuid4(), uuid4()

    lease_id = acquire_fetch_lease(conn, inv, "token-x", system_id=system_id, job_id=job_id)  # ty: ignore[invalid-argument-type]

    assert lease_id is not None
    sql, params = conn.statements[0]
    assert "INSERT INTO rootfs_fetch_leases" in sql
    # The holder is written to the row, because the gate's whole predicate reads it back. A row that
    # named no job would be the holderless pin migration 0090 made unrepresentable.
    assert params == (lease_id, inv, "token-x", system_id, job_id)


def test_a_fetch_with_no_holding_job_stages_unleased_rather_than_pinning_forever(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A lease naming no job could not be fenced on anything, so it would pin its base until an
    # operator deleted the row — strictly worse than no lease, because it reads as protection in the
    # table an operator inspects. Degrading (not raising) follows _flocked_partial's ENOLCK
    # precedent: the reclaim reverts to its pre-ADR-0515 reach, which is rare and survivable, where
    # failing would turn this into a total uploaded-rootfs provisioning outage.
    conn = _RecordingConn()

    with caplog.at_level("WARNING"):
        lease = acquire_fetch_lease(conn, uuid4(), "t", system_id=uuid4(), job_id=None)  # ty: ignore[invalid-argument-type]

    assert lease is None

    assert conn.statements == []
    assert "no provision job id reached the rootfs fetch" in caplog.text


def test_an_acquire_fault_degrades_to_no_lease_rather_than_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The pin is advisory. Failing here would turn a transient database blip into a total
    # uploaded-rootfs provisioning outage, where degrading only reverts the reclaim to its
    # pre-ADR-0515 reach. None is the caller's signal that there is nothing to release.
    conn = _RecordingConn(fault=True)

    with caplog.at_level("WARNING"):
        lease = acquire_fetch_lease(conn, uuid4(), "t", system_id=uuid4(), job_id=uuid4())  # ty: ignore[invalid-argument-type]

    assert lease is None
    # Reported, because the log line is the only evidence the degrade fired — the provision
    # succeeds either way.
    assert "staging unleased" in caplog.text


def test_a_release_fault_is_reported_and_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    # release_fetch_lease runs from the fetch's `finally`. Raising there would REPLACE the in-flight
    # exception, demoting an actionable CategorizedError — a checksum mismatch, a non-qcow2 upload —
    # to __context__ behind a Postgres message. That is the defect _release_fetch_lock documents for
    # the advisory lock one call away, and it arrives here for the same reason: on the crash shape
    # the fence exists to bound, the connection is frequently already gone by the time this runs.
    conn = _RecordingConn(fault=True)

    with caplog.at_level("WARNING"):
        release_fetch_lease(conn, uuid4())  # ty: ignore[invalid-argument-type]

    # The remedy is named: the row is not orphaned forever, it lapses with its holding job.
    assert "until its holding job stops being a live claim" in caplog.text


def test_a_non_autocommit_connection_records_no_lease(caplog: pytest.LogCaptureFixture) -> None:
    # The one way ADR-0515 could fail silently and totally. Inside a transaction the row is
    # invisible to the reclaim's separate connection until commit — on the production path, after
    # the download the lease exists to protect has already finished. The fetch would still succeed
    # and nothing would raise, so the mechanism would be a no-op that every test still passed.
    # Recording a lease that pins nothing is strictly worse than recording none: it would read as
    # protection in the table an operator inspects.
    conn = _RecordingConn(autocommit=False)

    with caplog.at_level("WARNING"):
        lease = acquire_fetch_lease(conn, uuid4(), "t", system_id=uuid4(), job_id=uuid4())  # ty: ignore[invalid-argument-type]

    assert lease is None
    assert conn.statements == []
    assert "not in autocommit" in caplog.text
