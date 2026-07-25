"""The per-investigation staging-dir sweep the reclaim handler runs once a drain completes.

A crash-orphaned ``<token>.*.partial`` no row owns is unlinked before the empty-dir removal (else
it keeps the dir non-empty forever); a dir still holding a base is left in place (ADR-0441 §5).

Since ADR-0452 (#1544) the unlink is gated on the same ``flock`` liveness test the fetch-side sweep
uses: this sweep runs only once no rootfs row remains, but that state is reached by a *classifier*
(``rootfs_base_reclaimable``, which reads the System's state column) that ``PROVISIONING ->
TORN_DOWN`` falsifies, so "no row remains" is not evidence that no fetcher is still writing. The
sweep reports whether a **live-held** partial was left behind, which is what decides the drain
marker.
"""

from __future__ import annotations

import fcntl
import multiprocessing as mp
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from kdive.jobs.handlers.artifacts.rootfs_reclaim import sweep_investigation_staging_dir

_TOKEN = "dGVzdC10b2tlbg"  # an arbitrary base64url content-address token


@contextmanager
def _held_partial(inv_dir: Path, token: str = _TOKEN) -> Iterator[Path]:
    """A ``<token>.<uuid>.partial`` under an exclusive ``flock``, as a live fetcher holds it.

    Stands in for the detached ``asyncio.to_thread`` download that keeps writing after a
    ``PROVISIONING -> TORN_DOWN`` transition dropped the base's pin: from the sweeper's point of
    view every rootfs row is gone and this file is present, which is the state ADR-0441 §5 and
    ADR-0442 §7 both assumed could not occur.
    """
    partial = inv_dir / f"{token}.{uuid4().hex}.partial"
    partial.write_bytes(b"a live fetcher is still writing this")
    fd = os.open(partial, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield partial
    finally:
        os.close(fd)


def _inv_dir(tmp_path: Path) -> tuple[UUID, Path]:
    inv = uuid4()
    inv_dir = tmp_path / str(inv)
    inv_dir.mkdir(parents=True)
    return inv, inv_dir


def test_partial_sweep_unlinks_and_removes_empty_dir(tmp_path: Path) -> None:
    # AC-8h: a crash-orphaned <token>.*.partial is swept before the now-empty dir is removed.
    inv, inv_dir = _inv_dir(tmp_path)
    orphan = inv_dir / f"{_TOKEN}.{uuid4().hex}.partial"
    orphan.write_bytes(b"partial")

    assert sweep_investigation_staging_dir(str(tmp_path), inv) is False

    assert not orphan.exists()
    assert not inv_dir.exists()  # empty after the partial swept -> removed


def test_partial_sweep_keeps_dir_holding_a_base(tmp_path: Path) -> None:
    inv, inv_dir = _inv_dir(tmp_path)
    (inv_dir / f"{_TOKEN}.{uuid4().hex}.partial").write_bytes(b"partial")
    base = inv_dir / f"{_TOKEN}.qcow2"
    base.write_bytes(b"base")  # a still-deferred base keeps the dir non-empty

    # A base is not a live writer: the dir survives the rmdir, but nothing is being waited on, so
    # the caller's drain marker must still clear.
    assert sweep_investigation_staging_dir(str(tmp_path), inv) is False

    assert inv_dir.exists()
    assert base.exists()
    assert not list(inv_dir.glob("*.partial"))  # partial still swept


def test_sweep_skips_a_partial_a_live_fetcher_still_holds(tmp_path: Path) -> None:
    # #1544's acceptance criterion, at the sweep itself. Red before ADR-0452: this sweep
    # glob-unlinked unconditionally, destroying an in-flight multi-GiB download whose System had
    # just been torn down or failed.
    inv, inv_dir = _inv_dir(tmp_path)

    with _held_partial(inv_dir) as live:
        assert sweep_investigation_staging_dir(str(tmp_path), inv) is True
        assert live.exists()
        assert live.read_bytes() == b"a live fetcher is still writing this"


def test_a_held_partial_keeps_the_staging_dir_and_is_reported(tmp_path: Path) -> None:
    # The rmdir interaction ADR-0452 §4 settles: a skipped live partial leaves the dir non-empty,
    # so the rmdir fails ENOTEMPTY. That is the achieved post-state, not a fault — and the returned
    # flag is what keeps the drain marker set so a later pass finishes the job.
    inv, inv_dir = _inv_dir(tmp_path)

    with _held_partial(inv_dir):
        assert sweep_investigation_staging_dir(str(tmp_path), inv) is True
        assert inv_dir.exists()


def test_sweep_unlinks_only_the_orphan_when_a_live_partial_sits_beside_it(tmp_path: Path) -> None:
    # This sweep globs *every* token in the investigation dir, not one base's, so an all-or-nothing
    # gate is the interesting failure: one that aborts on the first locked candidate strands the
    # orphan, one that ignores the lock destroys the live download.
    inv, inv_dir = _inv_dir(tmp_path)
    orphan = inv_dir / "b3RoZXItdG9rZW4.deadbeef.partial"
    orphan.write_bytes(b"leaked by a killed worker")

    with _held_partial(inv_dir) as live:
        assert sweep_investigation_staging_dir(str(tmp_path), inv) is True
        assert live.exists()
    assert not orphan.exists()


def test_sweep_tolerates_a_candidate_that_vanishes_between_the_glob_and_the_open(
    tmp_path: Path,
) -> None:
    # The fetch-side opportunistic sweep walks the same directory, so a candidate can be gone by the
    # time this one opens it. That is the achieved post-state, not a fault, and it must not read as
    # a live holder — reporting it as one would pin the drain marker on nothing.
    inv, inv_dir = _inv_dir(tmp_path)
    orphan = inv_dir / f"{_TOKEN}.deadbeef.partial"
    orphan.write_bytes(b"leaked")
    real_open = os.open

    def vanishing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == orphan:
            orphan.unlink()
        return real_open(path, flags, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "open", vanishing_open)
        assert sweep_investigation_staging_dir(str(tmp_path), inv) is False

    assert not orphan.exists()
    assert not inv_dir.exists()


def test_an_unopenable_partial_is_not_reported_as_live(tmp_path: Path) -> None:
    # ADR-0452 §4's other half. An EACCES/EROFS/EIO partial is permanent until an operator acts, so
    # reporting it as live would retain rootfs_cleanup_pending_at forever and resurrect exactly the
    # never-clearing-marker loop ADR-0442 was written to kill. Only a held flock — which the kernel
    # releases on process exit — pins the marker.
    inv, inv_dir = _inv_dir(tmp_path)
    unopenable = inv_dir / f"{_TOKEN}.deadbeef.partial"
    unopenable.write_bytes(b"present but not openable by this uid")
    real_open = os.open

    def refusing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == unopenable:
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "open", refusing_open)
        assert sweep_investigation_staging_dir(str(tmp_path), inv) is False

    assert unopenable.exists()  # left for an operator rather than unlinked unchecked


def _hold_flock_child(path: str, locked: Any) -> None:  # pragma: no cover - child process
    fd = os.open(path, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    locked.set()
    time.sleep(300)


def test_sweep_collects_a_partial_once_its_holder_process_dies(tmp_path: Path) -> None:
    # The property that makes an flock beat an mtime window, proven against a real process rather
    # than asserted — and the reason a *held* skip is safe to pin the drain marker on: the kernel
    # drops the lock when the holding descriptor closes, including on SIGKILL, so the retained
    # marker always converges rather than pinning the investigation forever.
    inv, inv_dir = _inv_dir(tmp_path)
    partial = inv_dir / f"{_TOKEN}.crashed.partial"
    partial.write_bytes(b"mid-download when the worker was killed")
    ctx = mp.get_context("spawn")
    locked = ctx.Event()
    proc = ctx.Process(target=_hold_flock_child, args=(str(partial), locked))
    proc.start()
    try:
        assert locked.wait(timeout=60), "the child never took the flock"
        assert sweep_investigation_staging_dir(str(tmp_path), inv) is True
        assert partial.exists(), "a live holder's partial was swept"
    finally:
        proc.kill()
        proc.join(timeout=60)
    assert proc.exitcode is not None, "the flock holder did not exit"

    assert sweep_investigation_staging_dir(str(tmp_path), inv) is False

    assert not partial.exists()
    assert not inv_dir.exists()


def test_sweep_of_an_absent_staging_dir_reports_nothing_live(tmp_path: Path) -> None:
    # An investigation that never staged anything: the glob finds nothing, the rmdir raises ENOENT
    # into the suppress, and the marker must still clear.
    assert sweep_investigation_staging_dir(str(tmp_path), uuid4()) is False
