"""The shared ``flock`` liveness gate both uploaded-rootfs partial sweeps unlink through.

The primitive itself, tested apart from either caller (ADR-0446 §4, ADR-0452 §1). Its per-outcome
behaviour is what both sweeps' safety rests on, and since ADR-0452 its return value also decides
whether the reclaim handler retains ``rootfs_cleanup_pending_at`` — so "held" and "could not
evaluate" must not collapse into one answer.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from kdive.providers.shared.staging_partials import unlink_partial_if_unheld


@contextmanager
def _flocked(partial: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``partial``, as a staging fetcher holds its own."""
    fd = os.open(partial, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _partial(tmp_path: Path, name: str = "token.deadbeef.partial") -> Path:
    partial = tmp_path / name
    partial.write_bytes(b"staged bytes")
    return partial


def _unlockable_flock(lock_errno: int) -> Any:
    """A ``fcntl.flock`` that always fails with ``lock_errno``, as a lock-less filesystem does."""

    def _flock(fd: int, operation: int) -> None:
        raise OSError(lock_errno, os.strerror(lock_errno))

    return _flock


def test_an_unheld_partial_is_unlinked(tmp_path: Path) -> None:
    partial = _partial(tmp_path)

    assert unlink_partial_if_unheld(partial, unlink_when_unlockable=False) is False

    assert not partial.exists()


def test_a_held_partial_survives_and_is_reported_held(tmp_path: Path) -> None:
    # The whole point of the gate: a live writer's file is not a crash orphan. The True is what a
    # caller waits on, and it is safe to wait on only because the kernel releases the lock when the
    # holding descriptor closes.
    partial = _partial(tmp_path)

    with _flocked(partial):
        assert unlink_partial_if_unheld(partial, unlink_when_unlockable=False) is True

    assert partial.read_bytes() == b"staged bytes"


def test_a_partial_that_already_vanished_is_not_a_fault(tmp_path: Path) -> None:
    # Both sweeps walk the same directory, so a candidate can be gone by the time this opens it.
    # That is the achieved post-state — and it must not read as a live holder, which would pin a
    # caller's drain marker on nothing.
    gone = tmp_path / "token.gone.partial"
    assert unlink_partial_if_unheld(gone, unlink_when_unlockable=False) is False


def test_a_partial_that_cannot_be_opened_is_left_and_warned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A narrowing of the unconditional unlink this replaces, which needed only write+execute on the
    # DIRECTORY. Deliberate — a partial this process cannot open is one it cannot show is dead — and
    # loud, because on the reclaim side there is no further backstop behind the skip. It is
    # emphatically *not* reported as held: an EACCES is permanent until an operator acts, so pinning
    # a drain marker on it would never clear (ADR-0452 §4).
    partial = _partial(tmp_path)
    real_open = os.open

    def denying_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == partial:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    with caplog.at_level(logging.WARNING), pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "open", denying_open)
        assert unlink_partial_if_unheld(partial, unlink_when_unlockable=False) is False

    assert partial.exists()
    assert any("could not open the staging partial" in r.getMessage() for r in caplog.records), (
        caplog.text
    )


@pytest.mark.parametrize("lock_errno", [errno.ENOLCK, errno.EOPNOTSUPP])
def test_an_unlockable_filesystem_skips_when_the_caller_asked_it_to(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, lock_errno: int
) -> None:
    # The fetch-side answer, ADR-0446 §4 unchanged: that sweep is opportunistic and bounded by
    # the next fetch, so it declines to unlink what it cannot evaluate and the backstop collects.
    partial = _partial(tmp_path)

    with caplog.at_level(logging.WARNING), pytest.MonkeyPatch.context() as patch:
        patch.setattr(fcntl, "flock", _unlockable_flock(lock_errno))
        assert unlink_partial_if_unheld(partial, unlink_when_unlockable=False) is False

    assert partial.exists()
    assert any(
        "could not test whether a live writer holds" in r.getMessage() for r in caplog.records
    ), caplog.text


@pytest.mark.parametrize("lock_errno", [errno.ENOLCK, errno.EOPNOTSUPP])
def test_an_unlockable_filesystem_collects_when_the_caller_is_the_last_collector(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, lock_errno: int
) -> None:
    # The reclaim-side answer. A filesystem that cannot flock could not flock for the *writer*
    # either -- _flocked_partial staged unguarded there -- so skipping protects nothing and would
    # retire the last collector for a SENSITIVE multi-GiB orphan on exactly the hosts where the
    # fetch-side gate had already degraded. Unlinking is that sweep's own pre-ADR-0446 behaviour,
    # confined to the case where the kernel refuses to answer.
    partial = _partial(tmp_path)

    with caplog.at_level(logging.WARNING), pytest.MonkeyPatch.context() as patch:
        patch.setattr(fcntl, "flock", _unlockable_flock(lock_errno))
        assert unlink_partial_if_unheld(partial, unlink_when_unlockable=True) is False

    assert not partial.exists()
    assert any("cannot flock the staging partial" in r.getMessage() for r in caplog.records), (
        caplog.text
    )


def test_a_per_candidate_lock_fault_is_never_treated_as_an_unlockable_filesystem(
    tmp_path: Path,
) -> None:
    # The distinction the degrade rests on. An EACCES/EIO from the flock itself says nothing about
    # the filesystem's ability to lock, so even the last collector leaves it alone -- folding the
    # two together is exactly the mistake the keyword exists to prevent.
    partial = _partial(tmp_path)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fcntl, "flock", _unlockable_flock(errno.EIO))
        assert unlink_partial_if_unheld(partial, unlink_when_unlockable=True) is False

    assert partial.exists()


def test_a_held_partial_is_never_unlinked_even_by_the_last_collector(tmp_path: Path) -> None:
    # unlink_when_unlockable must not become a back door around the gate itself: EWOULDBLOCK is
    # answered before the errno test, so a live writer is protected on every filesystem.
    partial = _partial(tmp_path)

    with _flocked(partial):
        assert unlink_partial_if_unheld(partial, unlink_when_unlockable=True) is True

    assert partial.read_bytes() == b"staged bytes"


def test_an_unlinkable_partial_is_warned_rather_than_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The unlink's own fault (EPERM under a sticky-bit or foreign-uid staging dir, EROFS, EIO) is
    # handled here, per candidate, so one bad file cannot abort the rest of a caller's pass.
    partial = _partial(tmp_path)
    real_unlink = os.unlink

    def refusing_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == partial:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        real_unlink(path, *args, **kwargs)

    with caplog.at_level(logging.WARNING), pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "unlink", refusing_unlink)
        assert unlink_partial_if_unheld(partial, unlink_when_unlockable=False) is False

    assert partial.exists()
    assert any("could not unlink the staging partial" in r.getMessage() for r in caplog.records), (
        caplog.text
    )
