"""Reclaim for objects a handler wrote but did not register (ADR-0519, #1725).

A worker handler that writes its object *outside* the advisory lock and registers the row
*inside* it can find the lock's guard refusing the registration — the job was canceled, or the
System left its live state — after the object has already landed. Nothing else reclaims that
object: every System-artifact sweep is **row-driven** (``_reclaim_console_artifacts`` and
``_reclaim_sysrq_artifacts`` in ``kdive.jobs.handlers.systems`` select ``object_key`` *from the
``artifacts`` rows*), so an object with no row is invisible to teardown and permanent. This
module is the compensating delete that closes that gap.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from kdive.domain.errors import CategorizedError
from kdive.store.objectstore import ObjectStore

_log = logging.getLogger(__name__)


async def discard_unregistered_objects(store: ObjectStore, keys: Sequence[str]) -> None:
    """Delete objects this attempt wrote and could not register; never raises.

    Call only after the caller's advisory lock has been released (the delete is object-store
    I/O, which is the very thing ADR-0519 keeps out of a locked span), and only for keys that
    no committed ``artifacts`` row references — a key a row already owns belongs to that row,
    not to this attempt.

    Best-effort: a store fault is logged and swallowed, because the caller is already on an
    abort path whose own outcome (a canceled job, a changed-state error) is the result that
    matters. A swallowed fault leaves exactly the orphan this call exists to prevent, so it is
    logged at warning with the key.

    Args:
        store: The object store the attempt wrote to.
        keys: Object keys written by this attempt that no ``artifacts`` row references.
    """
    for key in keys:
        try:
            await asyncio.to_thread(store.delete, key)
        except CategorizedError:
            _log.warning(
                "deleting unregistered object %s failed; it has no artifacts row, so no "
                "reclaim sweep will reach it",
                key,
                exc_info=True,
            )
