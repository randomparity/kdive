"""The ``flock`` liveness test the uploaded-rootfs partial paths share (ADR-0446/0452/0495).

A ``<token>.<uuid>.partial`` is written by exactly one fetcher and is a SENSITIVE multi-GiB file no
``artifacts`` row owns. Three places ask about one: the opportunistic sweep on the next fetch of
that base (``upload_staging._unlink_orphan_partials``), the reclaim-side backstop when the
investigation drains (``jobs.handlers.artifacts.rootfs_reclaim.sweep_investigation_staging_dir``),
and the row-driven reclaim's per-checksum gate
(``rootfs_reclaim._live_writer_holds_a_partial``). The first two **collect** the file; the third
only
**reads** it, which is why this module exposes two mappings over one shared probe.

None of them can tell a crash orphan from a download in flight by looking at the filesystem, and
each originally derived the distinction from state it holds: the fetch side from its
per-(investigation, checksum) advisory lock, the drain sweep from "no rootfs row remains for this
investigation", and the row-driven reclaim from the System-state pin classifier. All three
derivations are false — a session
advisory lock belongs to a Postgres *connection* that is idle for the whole download and can be
reaped from under a live writer (ADR-0446), and both of the others rest on a classifier that
``PROVISIONING -> TORN_DOWN`` falsifies (ADR-0452, ADR-0495).

So liveness is asked of the kernel instead. A live writer holds an exclusive ``flock`` on its own
partial for the whole download-verify-publish window (``upload_staging._flocked_partial``), and
a candidate that cannot be locked is a download still in flight. :func:`_probed` is that test,
shared rather than duplicated so the callers cannot drift on what the kernel's answers *mean*, and
placed under ``providers.shared`` because one caller is a job handler and ``src/kdive/jobs/`` must
not reach into a provider's lifecycle package.

What differs between the callers is only the mapping from answer to **action**, and they genuinely
disagree on three of five answers — most sharply on a filesystem that cannot ``flock`` at all, where
the writer also staged unguarded. That is each caller's answer, not this module's: a collector that
skips there collects nothing at all, while a gate that unlinks there destroys a possibly-live
writer's only copy.
"""

from __future__ import annotations

import enum
import errno
import fcntl
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_log = logging.getLogger(__name__)


#: The ``flock`` failures that say **this filesystem cannot lock at all**, as opposed to a fault on
#: this one candidate. They are the premise of ``_flocked_partial``'s own degrade branch (ADR-0446
#: §5), which names exactly these two: ``ENOLCK`` on an NFS mount whose lock manager is down, and
#: ``EOPNOTSUPP`` (``ENOTSUP`` on Linux) on some FUSE and 9p backends. On such a host the writer
#: staged unguarded, so the lock protocol carries no information in either direction and a sweep
#: that skips is not being careful — it is collecting nothing at all.
_UNLOCKABLE_FILESYSTEM_ERRNOS = frozenset({errno.ENOLCK, errno.EOPNOTSUPP})


class _Liveness(enum.Enum):
    """What the kernel said about one candidate — five answers rather than a ``bool``.

    A ``bool`` is enough for a *collector*, where "not provably live" and "could not be evaluated"
    call for the same inaction: leave the file alone. It stops being enough once a caller uses the
    answer to license deleting something **else** — the ADR-0495 reclaim gate deletes a staged base,
    an object-store object and an ``artifacts`` row — because such a caller must be able to say
    which answers are *provably* transient and which are permanent until an operator acts. Both
    mappings
    below are written in those terms rather than in each other's.
    """

    #: ``EWOULDBLOCK``: a live writer holds the ``flock``. The one *provably transient* answer,
    #: since the kernel drops it when the holding descriptor closes, including on ``SIGKILL``.
    HELD = "held"
    #: Locked successfully, so no writer holds it: a crash orphan.
    UNHELD = "unheld"
    #: Gone between the walk and the ``open`` — the achieved post-state, not a fault.
    ABSENT = "absent"
    #: This candidate could not be evaluated (``EACCES``, ``EMFILE``, ``EIO``). **Not** evidence of
    #: absence: a partial this process cannot open is one it cannot show is dead.
    UNEVALUABLE = "unevaluable"
    #: :data:`_UNLOCKABLE_FILESYSTEM_ERRNOS`: no file here can be locked, *including by the writer*,
    #: so the protocol carries no information in either direction.
    UNLOCKABLE = "unlockable"


@contextmanager
def _probed(partial: Path) -> Iterator[_Liveness]:
    """Classify ``partial``'s ``flock`` state, holding the descriptor for the caller's block.

    The descriptor stays open — and on :attr:`_Liveness.UNHELD` the lock stays *held* — for the
    duration of the ``with``, so a collector's ``unlink`` runs under the same lock acquisition that
    decided the file was dead rather than after releasing it.

    Every branch logs the **observation** and nothing else. What to do about it belongs to the
    caller, and naming an action here is how the fetch-side text came to assert a lost Postgres
    session on a path that has no fetch lock at all — writing a conditional down as an invariant,
    which is ``_release_fetch_lock``'s own stated principle in this same subsystem. A caller whose
    decision needs its own line emits one (``_reclaim_one_checksum`` does).

    ``O_NONBLOCK`` is a no-op on a regular file and is there for the reason ADR-0443 §2 checks
    ``S_ISREG`` before opening a staged base: opening a FIFO for reading blocks until a writer
    appears, and the fetch-side sweep runs *holding* the fetch advisory lock, so a hang would wedge
    every sibling System on that (investigation, checksum). Nothing in kdive creates a non-regular
    file at a ``.partial`` path — this must simply not acquire a way to hang that it did not have
    before.
    """
    try:
        fd = os.open(partial, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        yield _Liveness.ABSENT
        return
    except OSError as err:
        _log.warning(
            "could not open the staging partial %s to test whether a live writer holds it (%s)",
            partial,
            err.strerror,
        )
        yield _Liveness.UNEVALUABLE
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _log.warning(
                "skipping the staging partial %s: a live writer holds its flock, so it is a "
                "download still in flight rather than a crash orphan — it is left in place and is "
                "collectable as soon as its holder's descriptor closes",
                partial,
            )
            yield _Liveness.HELD
            return
        except OSError as err:
            if err.errno in _UNLOCKABLE_FILESYSTEM_ERRNOS:
                _log.warning(
                    "this filesystem cannot flock the staging partial %s (%s), so no writer "
                    "here holds one either and the liveness gate cannot exist on it",
                    partial,
                    err.strerror,
                )
                yield _Liveness.UNLOCKABLE
                return
            _log.warning(
                "could not test whether a live writer holds the staging partial %s (%s)",
                partial,
                err.strerror,
            )
            yield _Liveness.UNEVALUABLE
            return
        yield _Liveness.UNHELD
    finally:
        os.close(fd)


def live_writer_holds_partial(partial: Path) -> bool:
    """Report only a provably held writer lock, without mutating the partial (ADR-0495)."""
    with _probed(partial) as liveness:
        return liveness is _Liveness.HELD


def unlink_partial_if_unheld(partial: Path, *, unlink_when_unlockable: bool) -> bool:
    """Unlink an unheld partial and report whether a live writer kept it.

    On filesystems without flock support, ``unlink_when_unlockable`` selects the caller's explicit
    policy. Unevaluable and already-absent candidates are retained or ignored and reported false.
    """
    with _probed(partial) as liveness:
        if liveness is _Liveness.HELD:
            return True
        if liveness is _Liveness.ABSENT or liveness is _Liveness.UNEVALUABLE:
            return False
        if liveness is _Liveness.UNLOCKABLE:
            if not unlink_when_unlockable:
                return False
            _log.warning(
                "unlinking the staging partial %s as this sweep did before the liveness gate was "
                "added, because this host cannot answer the question and this is its last "
                "collector",
                partial,
            )
        try:
            partial.unlink(missing_ok=True)
        except OSError as err:
            _log.warning(
                "could not unlink the staging partial %s (%s); leaving it in place",
                partial,
                err.strerror,
            )
    return False
