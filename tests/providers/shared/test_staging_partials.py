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


def test_an_unheld_partial_is_unlinked(tmp_path: Path) -> None:
    partial = _partial(tmp_path)

    assert unlink_partial_if_unheld(partial) is False

    assert not partial.exists()


def test_a_held_partial_survives_and_is_reported_held(tmp_path: Path) -> None:
    # The whole point of the gate: a live writer's file is not a crash orphan. The True is what a
    # caller waits on, and it is safe to wait on only because the kernel releases the lock when the
    # holding descriptor closes.
    partial = _partial(tmp_path)

    with _flocked(partial):
        assert unlink_partial_if_unheld(partial) is True

    assert partial.read_bytes() == b"staged bytes"


def test_a_partial_that_already_vanished_is_not_a_fault(tmp_path: Path) -> None:
    # Both sweeps walk the same directory, so a candidate can be gone by the time this opens it.
    # That is the achieved post-state — and it must not read as a live holder, which would pin a
    # caller's drain marker on nothing.
    assert unlink_partial_if_unheld(tmp_path / "token.gone.partial") is False


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
        assert unlink_partial_if_unheld(partial) is False

    assert partial.exists()
    assert any("could not open the staging partial" in r.getMessage() for r in caplog.records), (
        caplog.text
    )


def test_a_lock_test_that_faults_leaves_the_partial_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # ENOLCK on a filesystem that cannot lock at all: the liveness question has no answer, so the
    # file is left rather than unlinked blind, and the WARNING is what keeps such a host from
    # silently retiring both sweeps.
    partial = _partial(tmp_path)

    def unlockable_flock(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    with caplog.at_level(logging.WARNING), pytest.MonkeyPatch.context() as patch:
        patch.setattr(fcntl, "flock", unlockable_flock)
        assert unlink_partial_if_unheld(partial) is False

    assert partial.exists()
    assert any(
        "could not test whether a live writer holds" in r.getMessage() for r in caplog.records
    ), caplog.text


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
        assert unlink_partial_if_unheld(partial) is False

    assert partial.exists()
    assert any("could not unlink the staging partial" in r.getMessage() for r in caplog.records), (
        caplog.text
    )
