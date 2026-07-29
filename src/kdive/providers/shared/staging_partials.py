"""The ``flock`` liveness gate the uploaded-rootfs partial sweeps unlink through (ADR-0446/0452).

A ``<token>.<uuid>.partial`` is written by exactly one fetcher and is a SENSITIVE multi-GiB file no
``artifacts`` row owns, so three call sites collect it: the opportunistic one on the next fetch of
that base (``rootfs_upload_fetch._unlink_orphan_partials``), the reclaim-side backstop when the
investigation drains (``jobs.handlers.artifacts.rootfs_reclaim.sweep_investigation_staging_dir``),
and the row-driven reclaim's own per-checksum probe
(``rootfs_reclaim._live_writer_holds_a_partial``), which reads the ``True`` return as "defer this
checksum" and unlinks the crash orphans it passes over on the way.

None can tell a crash orphan from a download in flight by looking at the filesystem, and each
originally derived the distinction from state it holds: the fetch side from its per-(investigation,
checksum) advisory lock, the drain sweep from "no rootfs row remains for this investigation", and
the row-driven reclaim from the System-state pin classifier. All three derivations are false — a
session advisory lock belongs to a Postgres *connection* that is idle for the whole download and can
be reaped from under a live writer (ADR-0446), and both of the others rest on a classifier that
``PROVISIONING -> TORN_DOWN`` falsifies (ADR-0452, ADR-0495).

So liveness is asked of the kernel instead. A live writer holds an exclusive ``flock`` on its own
partial for the whole download-verify-publish window
(``rootfs_upload_fetch._flocked_partial``), and a candidate a sweep cannot lock is skipped. This
module is that test, shared rather than duplicated so the callers cannot drift, and placed under
``providers.shared`` because one caller is a job handler and ``src/kdive/jobs/`` must not reach into
a provider's lifecycle package.

What a sweep does where the gate **cannot exist** — a filesystem that cannot ``flock`` at all, on
which the writer also staged unguarded — is the callers' answer, not this module's, because they
differ: the fetch-side sweep is opportunistic and skipping there costs a bounded delay, while the
reclaim-side callers are the last collector and skipping there retires it outright.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
from pathlib import Path

_log = logging.getLogger(__name__)


#: The ``flock`` failures that say **this filesystem cannot lock at all**, as opposed to a fault on
#: this one candidate. They are the premise of ``_flocked_partial``'s own degrade branch (ADR-0446
#: §5), which names exactly these two: ``ENOLCK`` on an NFS mount whose lock manager is down, and
#: ``EOPNOTSUPP`` (``ENOTSUP`` on Linux) on some FUSE and 9p backends. On such a host the writer
#: staged unguarded, so the lock protocol carries no information in either direction and a sweep
#: that skips is not being careful — it is collecting nothing at all.
_UNLOCKABLE_FILESYSTEM_ERRNOS = frozenset({errno.ENOLCK, errno.EOPNOTSUPP})


def unlink_partial_if_unheld(partial: Path, *, unlink_when_unlockable: bool) -> bool:
    """Unlink ``partial`` unless a live writer holds its ``flock``; every skip is logged.

    Args:
        partial: The ``<token>.<uuid>.partial`` candidate a sweep found.
        unlink_when_unlockable: What to do when the filesystem cannot ``flock`` at all
            (:data:`_UNLOCKABLE_FILESYSTEM_ERRNOS`). Deliberately has **no default** and is
            answered per call site, because the question is "what happens when this gate cannot
            exist" and inheriting an answer is how the gap below was created.
            ``False`` for the fetch-side opportunistic sweep, which keeps ADR-0446 §4's conservative
            skip: it is bounded by the next fetch and something else collects. ``True`` for both
            reclaim-side callers, where nothing else collects — skipping there would silently
            retire the last collector for a SENSITIVE multi-GiB orphan on exactly the hosts where
            the fetch-side gate had already degraded, which is strictly worse than the
            pre-ADR-0446 behaviour it would be replacing. ``True`` **is** that pre-ADR-0446
            behaviour, unchanged, applied only where the kernel refuses to answer.

    Returns:
        ``True`` when a live writer's ``flock`` kept the file, ``False`` in every other case —
        unlinked, already gone, or left behind because it could not be evaluated. Only the ``True``
        case is *provably transient*, since the kernel releases an ``flock`` when the holding
        descriptor closes, including on process exit, normal or ``SIGKILL``. That is what makes it
        the one outcome a caller may safely wait on, and both reclaim-side readers rest on exactly
        that: ADR-0452 §4 retains the reclaim drain marker on it and clears the marker on everything
        else, and ADR-0495 defers a whole checksum on it and reclaims on everything else — so a
        permanently unsweepable file cannot pin an investigation, or a base, forever.

    Five outcomes, deliberately not collapsed into one silent ``return`` (ADR-0446 §4):

    *Held.* ``EWOULDBLOCK`` means a writer is still staging this partial, and the skip is the
    correct action. It is also frequently the **only** externally visible symptom of the condition
    that produced it — on the fetch side a lost session lock, whose other consequence is a redundant
    multi-GiB download that reads as ordinary slowness; on the reclaim side a pin-dropping System
    transition, whose other consequences are a deferred drain (ADR-0452) and, since ADR-0495, a
    deferred checksum that its caller logs in its own right. It is logged for the same reason
    ADR-0443 §4 logs a rejected base: the operation succeeds, so the log line is the only evidence
    it fired.

    The message reports what was **observed** rather than either caller's inferred cause. The
    fetch-side text used to assert a lost Postgres session, which is simply false on the reclaim
    path where no fetch lock exists at all; naming the observation keeps this from writing a
    conditional down as an invariant, which is ``_release_fetch_lock``'s own stated principle in
    this same subsystem and the defect class both ADRs exist to remove.

    *Cannot evaluate this candidate.* ``EACCES`` under a uid asymmetry of the shape ADR-0442
    documents in this same subsystem, ``EMFILE`` under descriptor exhaustion (likeliest exactly when
    many stagings are in flight), a transient ``EIO``. This is a **narrowing** of the unconditional
    ``unlink`` it replaces, which needed only write and execute on the *directory* and no permission
    on the file. A partial this process cannot even open is one it cannot show is dead, and
    unlinking it anyway is the bug being fixed. ``WARNING``, because on the reclaim side there is no
    further backstop behind this skip.

    *Cannot lock at all.* :data:`_UNLOCKABLE_FILESYSTEM_ERRNOS` is a different condition from the
    one above and must not be folded into it, which is the mistake this argument exists to prevent.
    It is not "this candidate resists evaluation" but "no file on this filesystem can be evaluated,
    including by the writer" — the exact premise ``_flocked_partial`` degrades on, staging *without*
    a lock. Skipping there protects nothing and only decides which sweep stops collecting, so the
    caller answers it via ``unlink_when_unlockable`` rather than inheriting a policy.

    *Absent.* A candidate that vanishes between the glob and the ``open`` is the achieved
    post-state, not a fault — every caller walks the same directory.

    *Unlinkable.* The ``unlink``'s own fault, likeliest ``EPERM`` under a sticky-bit or foreign-uid
    staging directory, plus ``EROFS`` and ``EIO``. Handled here, per candidate, so one bad file
    cannot abort the rest of a caller's pass.

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
        return False
    except OSError as err:
        _log.warning(
            "could not open the staging partial %s to test whether a live writer holds it (%s); "
            "leaving it in place rather than unlinking it unchecked",
            partial,
            err.strerror,
        )
        return False
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
            return True
        except OSError as err:
            if err.errno not in _UNLOCKABLE_FILESYSTEM_ERRNOS or not unlink_when_unlockable:
                _log.warning(
                    "could not test whether a live writer holds the staging partial %s (%s); "
                    "leaving it in place rather than unlinking it unchecked",
                    partial,
                    err.strerror,
                )
                return False
            _log.warning(
                "this host cannot flock the staging partial %s (%s), so no writer here holds one "
                "either and the liveness gate cannot exist; unlinking it as this sweep did before "
                "the gate was added, because it is the last collector for it",
                partial,
                err.strerror,
            )
        try:
            partial.unlink(missing_ok=True)
        except OSError as err:
            _log.warning(
                "could not unlink the staging partial %s (%s); leaving it in place",
                partial,
                err.strerror,
            )
    finally:
        os.close(fd)
    return False
