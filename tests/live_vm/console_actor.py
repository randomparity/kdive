"""The provisioned-family console-inode handoff, from the provisioning actor to this process.

ADR-0576 gives every System one console inode owned by the actor that starts its domain:
``storage._prepare_console_log`` opens the log before each start and fails the start unless the
opened inode is a regular, single-linked file owned by the starting process. The check is about
attribution, not readability — a peer-owned ``0664`` log is perfectly readable and still fails.

The live provisioned family has two actors for one System. ``scripts/live-vm/mint-system.sh``
provisions it through the MCP stack, so the fixed worker account creates the inode and performs
the provisioning start; the live tests then power-cycle that same System **in the pytest process**
under the operator account. Minting is where ownership transfers: once ``mint-system.sh`` returns
an id, this process is the System's sole starting actor for the rest of the run.

:func:`claim_console_inode` makes that transfer explicit at the moment it happens. It discards the
previous actor's inode so the seam mints a fresh one this process owns on the next start — the
same thing the seam already does for a System whose log has never existed. Nothing is relaxed
(``st_uid`` equality is still enforced, in ``storage.py``, unchanged), nothing privileged runs, and
the misattributed inode is removed rather than accepted.

**No evidence is lost by the discard, and the ADR's own title misleads on this point.** ADR-0576 is
"the worker owns the console-log inode *across boots*", but the bytes do not survive a boot: the
seam ends with ``os.ftruncate(fd, 0)`` (``storage.py:301``), the worker-side per-start truncate that
replaced virtlogd's ``append="off"`` truncation. The provisioning boot's bytes are discarded by the
next start whoever performs it, so unlinking here changes the inode's identity and nothing else.

The handoff is deliberately narrow: it is not autouse, it is called explicitly at the single site
where the actor changes, and nothing in ``src/`` imports it.
"""

from __future__ import annotations

import os
import stat
import warnings
from pathlib import Path
from uuid import UUID

from kdive.providers.shared.runtime_paths import console_log_path


def claim_console_inode(system_id: UUID) -> bool:
    """Take over ``system_id``'s console inode from whichever actor provisioned the System.

    Call this before starting the System's domain in-process, and only from a test that is the
    System's sole starting actor from here on: a worker job dispatched for the same System
    afterwards would (correctly) fail its own identity check on the inode this process leaves
    behind.

    A discard is announced as a warning naming the path and the uid it belonged to, so a live run
    shows on its own output that the handoff happened. Silence means nothing was claimed, which is
    indistinguishable from the call never being made — deliberately, since both are the same
    no-op.

    Returns:
        True when a foreign inode was discarded, False when there was nothing to claim — an
        absent log, one this process already owns, or one whose identity the seam must reject.
    """
    path = console_log_path(system_id)
    euid = os.geteuid()
    discarded_uid = _claim_console_inode(path, euid)
    if discarded_uid is None:
        return False
    warnings.warn(
        f"claimed the console inode of System {system_id}: discarded {path}, which belonged to "
        f"uid {discarded_uid}, so this process (uid {euid}) mints its own at the next domain "
        "start (ADR-0576 actor handoff)",
        stacklevel=2,
    )
    return True


def _claim_console_inode(path: Path, euid: int) -> int | None:
    """Unlink ``path`` when it is an ordinary file owned by an account other than ``euid``.

    Only the ordinary case is claimed. A symlink or a multi-linked path is a *different* unsafe
    identity, and discarding those would let a start proceed past an anomaly ADR-0576 exists to
    stop: replacing them with a fresh single-linked file would turn a loud failure into a silent
    success. They are left exactly where they are, for ``_prepare_console_log`` to reject by name.

    ``euid`` is a parameter rather than an internal ``os.geteuid()`` read so the ownership branch
    is provable without root and without a second account.

    Returns:
        The uid the discarded inode belonged to, or ``None`` when nothing was claimed.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid == euid:
        return None
    path.unlink()
    return st.st_uid
