"""The ``flock`` liveness gate both uploaded-rootfs partial sweeps unlink through (ADR-0446/0452).

A ``<token>.<uuid>.partial`` is written by exactly one fetcher and is a SENSITIVE multi-GiB file no
``artifacts`` row owns, so two sweeps collect it: the opportunistic one on the next fetch of that
base (``rootfs_upload_fetch._unlink_orphan_partials``) and the reclaim-side backstop when the
investigation drains (``jobs.handlers.artifacts.rootfs_reclaim.sweep_investigation_staging_dir``).

Neither can tell a crash orphan from a download in flight by looking at the filesystem, and both
originally derived the distinction from state they hold: the fetch side from its per-(investigation,
checksum) advisory lock, the reclaim side from "no rootfs row remains for this investigation".
Both derivations are false — a session advisory lock belongs to a Postgres *connection* that is idle
for the whole download and can be reaped from under a live writer (ADR-0446), and the row count
reaches zero via a System-state classifier that ``PROVISIONING -> TORN_DOWN`` falsifies (ADR-0452).

So liveness is asked of the kernel instead. A live writer holds an exclusive ``flock`` on its own
partial for the whole download-verify-publish window
(``rootfs_upload_fetch._flocked_partial``), and a candidate a sweep cannot lock is skipped. This
module is that test, shared rather than duplicated so the two sweeps cannot drift, and placed under
``providers.shared`` because one caller is a job handler and ``src/kdive/jobs/`` must not reach into
a provider's lifecycle package.
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path

_log = logging.getLogger(__name__)


def unlink_partial_if_unheld(partial: Path) -> bool:
    """Unlink ``partial`` unless a live writer holds its ``flock``; every skip is logged.

    Args:
        partial: The ``<token>.<uuid>.partial`` candidate a sweep found.

    Returns:
        ``True`` when a live writer's ``flock`` kept the file, ``False`` in every other case —
        unlinked, already gone, or left behind because it could not be evaluated. Only the ``True``
        case is *provably transient*, since the kernel releases an ``flock`` when the holding
        descriptor closes, including on process exit, normal or ``SIGKILL``. That is what makes it
        the one outcome a caller may safely wait on: ADR-0452 §4 retains the reclaim drain marker on
        it and clears the marker on everything else, so a permanently unsweepable file cannot pin an
        investigation forever.

    Four outcomes, deliberately not collapsed into one silent ``return`` (ADR-0446 §4):

    *Held.* ``EWOULDBLOCK`` means a writer is still staging this partial, and the skip is the
    correct action. It is also frequently the **only** externally visible symptom of the condition
    that produced it — on the fetch side a lost session lock, whose other consequence is a redundant
    multi-GiB download that reads as ordinary slowness; on the reclaim side a pin-dropping System
    transition, whose other consequence is invisible. It is logged for the same reason ADR-0443 §4
    logs a rejected base: the operation succeeds, so the log line is the only evidence it fired.

    The message reports what was **observed** rather than either caller's inferred cause. The
    fetch-side text used to assert a lost Postgres session, which is simply false on the reclaim
    path where no fetch lock exists at all; naming the observation keeps this from writing a
    conditional down as an invariant, which is ``_release_fetch_lock``'s own stated principle in
    this same subsystem and the defect class both ADRs exist to remove.

    *Cannot evaluate.* Any other ``OSError`` — ``EACCES`` under a uid asymmetry of the shape
    ADR-0442 documents in this same subsystem, ``EMFILE`` under descriptor exhaustion (likeliest
    exactly when many stagings are in flight), ``ENOLCK`` where the filesystem cannot lock at all —
    is a **narrowing** of the unconditional ``unlink`` this replaces, which needed only write and
    execute on the *directory* and no permission on the file. A partial this process cannot even
    open is one it cannot show is dead, and unlinking it anyway is the bug being fixed. ``WARNING``,
    because on the reclaim side there is no further backstop behind this skip.

    *Absent.* A candidate that vanishes between the glob and the ``open`` is the achieved
    post-state, not a fault — the two sweeps walk the same directory.

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
            _log.warning(
                "could not test whether a live writer holds the staging partial %s (%s); leaving "
                "it in place rather than unlinking it unchecked",
                partial,
                err.strerror,
            )
            return False
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
