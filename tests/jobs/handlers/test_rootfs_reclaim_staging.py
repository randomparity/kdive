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

import errno
import fcntl
import logging
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


def _sweep(
    uploads: Path,
    inv: UUID,
    *,
    protected: frozenset[str] = frozenset(),
    drained: bool = True,
) -> bool:
    """The sweep as the drain tail calls it, defaulting to the fully-drained investigation.

    ``protected_tokens`` empty and ``drained`` true is the post-state ADR-0452 assumed was the only
    one the sweep ever ran in; ADR-0494 made both explicit, and the cases that exercise the other
    values pass them.
    """
    return sweep_investigation_staging_dir(
        str(uploads), inv, protected_tokens=protected, drained=drained
    )


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

    assert _sweep(tmp_path, inv) is False

    assert not orphan.exists()
    assert not inv_dir.exists()  # empty after the partial swept -> removed


def test_a_base_left_after_the_drain_is_unowned_and_collected(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # ADR-0452 §6. The sweep runs only once no rootfs row remains for the investigation, so a base
    # still sitting here holds no row and no overlay can be backed by it — an overlay would have
    # pinned the row and ended the drain tail before this ran. It is the shape the flock gate makes
    # reachable: a doomed fetcher publishes onto a path its own reclaim already emptied. Leaving it
    # would trade the live-partial defect for a permanent SENSITIVE leak nothing else collects.
    inv, inv_dir = _inv_dir(tmp_path)
    (inv_dir / f"{_TOKEN}.{uuid4().hex}.partial").write_bytes(b"partial")
    base = inv_dir / f"{_TOKEN}.qcow2"
    base.write_bytes(b"published after its own row was reclaimed")

    with caplog.at_level(logging.WARNING):
        assert _sweep(tmp_path, inv) is False

    assert not base.exists()
    assert not inv_dir.exists()  # the dir now actually drains, so the rmdir stops failing silently
    assert any("outlived the artifacts row" in r.getMessage() for r in caplog.records), caplog.text


def test_an_unowned_base_that_cannot_be_unlinked_is_warned_not_raised(tmp_path: Path) -> None:
    # Per candidate, like the partial loop: one bad file must not abort the pass or raise into the
    # handler, which would fail a job whose reclaim already succeeded.
    inv, inv_dir = _inv_dir(tmp_path)
    base = inv_dir / f"{_TOKEN}.qcow2"
    base.write_bytes(b"base")
    real_unlink = os.unlink

    def refusing_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == base:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        real_unlink(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "unlink", refusing_unlink)
        assert _sweep(tmp_path, inv) is False

    assert base.exists()
    assert inv_dir.exists()


def test_sweep_skips_a_partial_a_live_fetcher_still_holds(tmp_path: Path) -> None:
    # #1544's acceptance criterion, at the sweep itself. Red before ADR-0452: this sweep
    # glob-unlinked unconditionally, destroying an in-flight multi-GiB download whose System had
    # just been torn down or failed.
    inv, inv_dir = _inv_dir(tmp_path)

    with _held_partial(inv_dir) as live:
        assert _sweep(tmp_path, inv) is True
        assert live.exists()
        assert live.read_bytes() == b"a live fetcher is still writing this"


def test_a_held_partial_keeps_the_staging_dir_and_is_reported(tmp_path: Path) -> None:
    # The rmdir interaction ADR-0452 §4 settles: a skipped live partial leaves the dir non-empty,
    # so the rmdir fails ENOTEMPTY. That is the achieved post-state, not a fault — and the returned
    # flag is what keeps the drain marker set so a later pass finishes the job.
    inv, inv_dir = _inv_dir(tmp_path)

    with _held_partial(inv_dir):
        assert _sweep(tmp_path, inv) is True
        assert inv_dir.exists()


def test_sweep_unlinks_only_the_orphan_when_a_live_partial_sits_beside_it(tmp_path: Path) -> None:
    # This sweep globs *every* token in the investigation dir, not one base's, so an all-or-nothing
    # gate is the interesting failure: one that aborts on the first locked candidate strands the
    # orphan, one that ignores the lock destroys the live download.
    inv, inv_dir = _inv_dir(tmp_path)
    orphan = inv_dir / "b3RoZXItdG9rZW4.deadbeef.partial"
    orphan.write_bytes(b"leaked by a killed worker")

    with _held_partial(inv_dir) as live:
        assert _sweep(tmp_path, inv) is True
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
        assert _sweep(tmp_path, inv) is False

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
        assert _sweep(tmp_path, inv) is False

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
        assert _sweep(tmp_path, inv) is True
        assert partial.exists(), "a live holder's partial was swept"
    finally:
        proc.kill()
        proc.join(timeout=60)
    assert proc.exitcode is not None, "the flock holder did not exit"

    assert _sweep(tmp_path, inv) is False

    assert not partial.exists()
    assert not inv_dir.exists()


@pytest.mark.parametrize("lock_errno", [errno.ENOLCK, errno.EOPNOTSUPP])
def test_a_lockless_filesystem_still_collects_and_still_drains(
    tmp_path: Path, lock_errno: int
) -> None:
    # ADR-0452 §2. On a filesystem that cannot flock, _flocked_partial stages *unguarded*, so no
    # writer holds a lock and the gate carries no information. Skipping here would make this sweep
    # -- the last collector -- collect nothing at all and still clear the drain marker, which is a
    # regression against the unconditional unlink it replaces rather than a safety improvement.
    inv, inv_dir = _inv_dir(tmp_path)
    orphan = inv_dir / f"{_TOKEN}.deadbeef.partial"
    orphan.write_bytes(b"leaked on a host that cannot lock")

    def unlockable_flock(fd: int, operation: int) -> None:
        raise OSError(lock_errno, os.strerror(lock_errno))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fcntl, "flock", unlockable_flock)
        assert _sweep(tmp_path, inv) is False

    assert not orphan.exists()
    assert not inv_dir.exists()


def test_sweep_of_an_absent_staging_dir_reports_nothing_live(tmp_path: Path) -> None:
    # An investigation that never staged anything: the glob finds nothing, the rmdir raises ENOENT
    # into the suppress, and the marker must still clear.
    assert _sweep(tmp_path, uuid4()) is False


def test_a_held_partial_does_not_suppress_the_unowned_base_collection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The two new outcomes in one pass, which is the combination an obvious refactor breaks: an
    # early `if held: return True` before the base loop reads as "the directory is not drained, so
    # stop" and would silently reintroduce the permanent SENSITIVE leak ADR-0452 §6 closes, for any
    # base published between the two globs. The base is unowned whether or not someone is writing a
    # different partial, and unlinking it costs the live writer nothing -- it publishes anyway.
    inv, inv_dir = _inv_dir(tmp_path)
    base = inv_dir / f"{_TOKEN}.qcow2"
    base.write_bytes(b"published after its own row was reclaimed")

    with caplog.at_level(logging.WARNING), _held_partial(inv_dir) as live:
        assert _sweep(tmp_path, inv) is True
        assert live.exists()

    assert not base.exists()
    assert inv_dir.exists()  # the held partial still keeps the dir, so the marker is retained


def test_a_completion_marker_does_not_keep_the_staging_dir_alive(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # ADR-0451 section 6, and the whole reason ADR-0443 deferred the marker. A sidecar matching
    # neither *.partial nor *.qcow2 is invisible to both existing globs, so the rmdir below fails
    # ENOTEMPTY on EVERY drained investigation that ever staged a base -- leaking one directory
    # apiece. Since ADR-0452 section 7 it would also do it LOUDLY, firing the unexplained-survivor
    # WARNING on every ordinary drain and training an operator to ignore the one line that reports a
    # genuinely unreadable staging tree.
    inv, inv_dir = _inv_dir(tmp_path)
    marker = inv_dir / f"{_TOKEN}.ready"
    marker.touch()

    with caplog.at_level(logging.WARNING):
        assert _sweep(tmp_path, inv) is False

    assert not marker.exists()
    assert not inv_dir.exists()
    assert not any(
        "survived its investigation's drain" in r.getMessage() for r in caplog.records
    ), caplog.text


def test_the_marker_sweep_is_not_gated_on_an_flock(tmp_path: Path) -> None:
    # ADR-0451 section 6's load-bearing negative. `unlink_partial_if_unheld` answers ONE question --
    # is a live writer still holding this multi-GiB partial across a download -- and its True is the
    # only outcome ADR-0452 section 5 established is provably transient, which is why the caller
    # retains the drain marker on it and on nothing else. Routing the zero-byte completion marker
    # through it "for consistency" would let a marker pin an investigation's drain and would make a
    # leaked marker indistinguishable from a held partial at the rmdir. A marker whose flock is
    # already held by someone else must therefore still be collected.
    inv, inv_dir = _inv_dir(tmp_path)
    marker = inv_dir / f"{_TOKEN}.ready"
    marker.touch()
    fd = os.open(marker, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _sweep(tmp_path, inv) is False
    finally:
        os.close(fd)

    assert not marker.exists()
    assert not inv_dir.exists()


def test_a_marker_that_cannot_be_unlinked_is_warned_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Per candidate, like both loops beside it: one bad file must not abort the pass or raise into
    # the handler, which would fail a job whose reclaim already succeeded. And it is logged, because
    # this pass is the last collector -- ADR-0452 section 7's "no step is silent".
    inv, inv_dir = _inv_dir(tmp_path)
    marker = inv_dir / f"{_TOKEN}.ready"
    marker.touch()
    real_unlink = os.unlink

    def refusing_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == marker:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        real_unlink(path, *args, **kwargs)

    with caplog.at_level(logging.WARNING), pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "unlink", refusing_unlink)
        assert _sweep(tmp_path, inv) is False

    assert marker.exists()
    assert any("completion marker" in r.getMessage() for r in caplog.records), caplog.text


def test_a_held_partial_still_reports_live_with_a_marker_beside_it(tmp_path: Path) -> None:
    # The marker sweep must not perturb the flag the drain marker is keyed on. A held partial is
    # still the answer, the directory still survives, and collecting the marker in the same pass
    # neither flips the flag nor -- the opposite refactor -- makes an ordinary leaked marker report
    # as a live writer and pin rootfs_cleanup_pending_at on nothing.
    inv, inv_dir = _inv_dir(tmp_path)
    marker = inv_dir / f"{_TOKEN}.ready"
    marker.touch()

    with _held_partial(inv_dir) as live:
        assert _sweep(tmp_path, inv) is True
        assert live.exists()

    assert not marker.exists()
    assert inv_dir.exists()  # kept by the held partial alone


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 directory regardless")
def test_a_staging_dir_the_sweep_cannot_read_is_named_rather_than_read_as_drained(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Path.glob returns an empty iterator for a directory it cannot enumerate rather than raising,
    # so an unreadable staging tree yields the same False as an empty one -- and the caller then
    # clears rootfs_cleanup_pending_at and retires every collector this investigation has. The rmdir
    # is the only step that can tell the two apart, so it must not be silent.
    inv, inv_dir = _inv_dir(tmp_path)
    orphan = inv_dir / f"{_TOKEN}.deadbeef.partial"
    orphan.write_bytes(b"uncollected")
    base = inv_dir / f"{_TOKEN}.qcow2"
    base.write_bytes(b"uncollected")
    inv_dir.chmod(0o000)
    try:
        with caplog.at_level(logging.WARNING):
            assert _sweep(tmp_path, inv) is False
    finally:
        inv_dir.chmod(0o700)

    assert orphan.exists()  # nothing was collected ...
    assert base.exists()
    assert any("survived its investigation's drain" in r.getMessage() for r in caplog.records), (
        caplog.text  # ... and the pass said so
    )


def test_a_base_an_artifacts_row_still_owns_is_left_alone(tmp_path: Path) -> None:
    # ADR-0494 section 3. Once the sweep runs alongside surviving rows, the collection cannot be
    # licensed by "no row remains" any more -- it has to test each file's own token. An
    # unconditional glob here unlinks a live base out from under the row that owns it, and its
    # ADR-0451
    # marker too, which silently forces a multi-GiB re-download on the next provision.
    inv, inv_dir = _inv_dir(tmp_path)
    owned = inv_dir / f"{_TOKEN}.qcow2"
    owned.write_bytes(b"a row still owns this")
    owned_marker = inv_dir / f"{_TOKEN}.ready"
    owned_marker.touch()
    orphan = inv_dir / "b3RoZXItdG9rZW4.qcow2"
    orphan.write_bytes(b"no row owns this")
    orphan_marker = inv_dir / "b3RoZXItdG9rZW4.ready"
    orphan_marker.touch()

    assert _sweep(tmp_path, inv, protected=frozenset({_TOKEN}), drained=False) is True

    assert owned.exists()
    assert owned_marker.exists()
    assert not orphan.exists()
    assert not orphan_marker.exists()
    assert inv_dir.exists()  # a surviving row keeps the directory; the row lane is the follow-up


def test_a_pinned_but_unowned_base_is_left_reported_and_still_clears_the_marker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The one survivor a *drained* investigation may still have. Its token holds no artifacts row,
    # so the old zero-row licence would unlink it -- underneath the overlay of a System that is
    # still live. It must be left. It must NOT pin the drain marker: `_ROOTFS_REFERENCERS_SQL`
    # excludes only `torn_down`, `failed` is terminal with no transition out of it, and nothing
    # removes a failed System's overlay -- so the pin can be permanent, and retaining on it is the
    # never-clearing marker ADR-0442 was written to kill. An unprotected orphan sits beside it, so
    # this cannot pass on an implementation that returns early without walking the directory.
    inv, inv_dir = _inv_dir(tmp_path)
    pinned = inv_dir / f"{_TOKEN}.qcow2"
    pinned.write_bytes(b"a live System's overlay is backed by this")
    orphan = inv_dir / "b3RoZXItdG9rZW4.qcow2"
    orphan.write_bytes(b"no row and no pin")

    with caplog.at_level(logging.WARNING):
        assert _sweep(tmp_path, inv, protected=frozenset({_TOKEN}), drained=True) is False

    assert pinned.exists()
    assert not orphan.exists()
    assert inv_dir.exists()
    assert any("a live System pins" in r.getMessage() for r in caplog.records), caplog.text


def test_an_empty_dir_does_not_report_a_pinned_base_just_because_the_set_is_non_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # `protected_tokens` is routinely non-empty with nothing left on disk -- the row-driven reclaim
    # unlinks each base as its own row drains, so that is the ordinary steady state. Deriving "a
    # base was left behind" from the set rather than from the walk would fire the survivor WARNING
    # on every ordinary drain and, worse, defer a drain that has plainly completed.
    inv, inv_dir = _inv_dir(tmp_path)

    with caplog.at_level(logging.WARNING):
        assert _sweep(tmp_path, inv, protected=frozenset({_TOKEN}), drained=True) is False

    assert not inv_dir.exists()
    assert not caplog.records, caplog.text


def test_a_base_published_between_the_glob_and_the_rmdir_is_collected_by_the_repass(
    tmp_path: Path,
) -> None:
    # The window ADR-0494 section 4 closes. The caller clears the drain marker on a False, retiring
    # every collector this investigation has, so a base that lands after this pass's own globs is a
    # permanent SENSITIVE leak -- exactly #1559's shape. One bounded re-pass converts it into an
    # extra readdir. Without the re-pass the base survives and the directory never drains.
    inv, inv_dir = _inv_dir(tmp_path)
    base = inv_dir / f"{_TOKEN}.qcow2"
    real_rmdir = os.rmdir
    published: list[int] = []

    def publishing_rmdir(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == inv_dir and not published:
            published.append(1)
            base.write_bytes(b"a doomed fetcher published after the globs ran")
        real_rmdir(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "rmdir", publishing_rmdir)
        assert _sweep(tmp_path, inv) is False

    assert published, "the rmdir seam never fired, so this proved nothing"
    assert not base.exists()
    assert not inv_dir.exists()


def test_the_repass_is_bounded_at_one_and_still_clears_the_marker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The boundedness half. An unbounded retry is the never-terminating drain ADR-0452 section 5
    # rejected, so a directory that keeps refilling must stop after one extra pass, warn, and return
    # False -- letting the caller clear the marker rather than pinning the investigation forever.
    inv, inv_dir = _inv_dir(tmp_path)
    real_rmdir = os.rmdir
    calls: list[int] = []

    def refilling_rmdir(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == inv_dir:
            calls.append(1)
            (inv_dir / f"{_TOKEN}.qcow2").write_bytes(b"published again")
        real_rmdir(path, *args, **kwargs)

    with caplog.at_level(logging.WARNING), pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "rmdir", refilling_rmdir)
        assert _sweep(tmp_path, inv) is False

    assert len(calls) == 2, "the re-pass must run exactly once, not loop"
    assert any("survived its investigation's drain" in r.getMessage() for r in caplog.records), (
        caplog.text
    )


def test_a_surviving_row_defers_without_warning_or_removing_the_dir(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A pinned base with its row intact is the expected steady state for the whole grace window, so
    # this path must stay quiet: warning on it every pass would bury the line that reports a real
    # survivor. It must also not rmdir -- the row lane is still going to use this directory.
    inv, inv_dir = _inv_dir(tmp_path)

    with caplog.at_level(logging.WARNING):
        assert _sweep(tmp_path, inv, protected=frozenset({_TOKEN}), drained=False) is True

    assert inv_dir.exists()
    assert not caplog.records, caplog.text


def test_a_surviving_row_leaves_the_partial_glob_alone(tmp_path: Path) -> None:
    # ADR-0494 section 2's reach limit. The token-keyed collectors run while rows survive, but the
    # partial glob does not: its candidates are decided by the flock gate rather than by ownership,
    # and a surviving row means a fetch of that base can legitimately be in flight. Sweeping here
    # would clobber it, which is ADR-0442 section 7's retained guarantee -- and widening it is
    # #1565's question, not this one's.
    inv, inv_dir = _inv_dir(tmp_path)
    in_flight = inv_dir / f"{_TOKEN}.{uuid4().hex}.partial"
    in_flight.write_bytes(b"an unheld partial of a base whose row survives")
    orphan = inv_dir / "b3RoZXItdG9rZW4.qcow2"
    orphan.write_bytes(b"no row owns this")

    assert _sweep(tmp_path, inv, protected=frozenset({_TOKEN}), drained=False) is True

    assert in_flight.exists()
    assert not orphan.exists()  # ... while the token-keyed collection still reaches the orphan
