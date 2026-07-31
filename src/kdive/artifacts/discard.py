"""Reclaim for objects a handler wrote but did not register (ADR-0519, #1725).

A worker handler that writes its object *outside* the advisory lock and registers the row
*inside* it can find the lock's guard refusing the registration — the job was canceled, or the
System left its live state — after the object has already landed. Nothing else reclaims that
object: every System-artifact sweep is **row-driven** (``_reclaim_console_artifacts`` and
``_reclaim_sysrq_artifacts`` in ``kdive.jobs.handlers.systems`` select ``object_key`` *from the
``artifacts`` rows*), so an object with no row is invisible to teardown and permanent. This
module is the compensating delete that closes that gap.

The delete runs after the lock is released, so it cannot simply trust the row probe the locked
phase made. The worker tier is at-least-once by design — a lapsed lease lets a second attempt of
the same job run concurrently, which ``jobs/worker.py`` names outright when it rejects a
heartbeat interval that "risks mid-job reclaim and double-run" — and a guard such as
``SystemState.READY`` is not monotonic, so a peer attempt can register the very key this one is
about to delete. Each delete is therefore fenced twice, immediately before it: the row must
still be absent, and the object must still carry the etag *this* attempt wrote.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

from kdive.artifacts.storage import StoredArtifact
from kdive.domain.errors import CategorizedError
from kdive.store.objectstore import ObjectStore

_log = logging.getLogger(__name__)


async def discard_unregistered_objects(
    store: ObjectStore,
    written: Sequence[StoredArtifact],
    *,
    still_unregistered: Callable[[str], Awaitable[bool]],
) -> None:
    """Delete objects this attempt wrote and could not register; never raises.

    Call only after the caller's advisory lock has been released — the delete is object-store
    I/O, which is the very thing ADR-0519 keeps out of a locked span.

    Two fences guard each delete, evaluated per key as late as possible:

    1. The object still carries the etag this attempt wrote. A peer that replaced the bytes owns
       what is there now, so deleting it would destroy another attempt's object.
    2. ``still_unregistered`` — the caller's own ``artifacts`` row probe, re-run *outside* the
       lock. A row that appeared since the locked phase belongs to a peer attempt, and the
       object belongs to that row rather than to this attempt.

    In that order: the row probe is the authoritative fence, so it goes last, leaving only the
    delete call between it and the delete rather than a whole store round-trip.

    Neither fence is a proof: a peer can still commit its row inside the one store round-trip
    between the probe and the delete, and two attempts that write byte-identical content share
    an etag. What the fences buy is a window of one round-trip instead of the whole registration
    phase. Whether the interleaving is reachable at all depends on the caller's guard — a
    monotonic one (a canceled job never un-cancels) cannot produce it — so each call site states
    its own argument rather than deferring to this one.

    Best-effort: a store fault is logged and swallowed, because the caller is already on an
    abort path whose own outcome (a canceled job, a changed-state error) is the result that
    matters. A swallowed fault leaves exactly the orphan this call exists to prevent, and on a
    teardown path no later attempt will overwrite it, so it is logged at warning with the key.
    One unreachable object must not strand the others: each key is an independent orphan.

    Args:
        store: The object store this attempt wrote to.
        written: What this attempt stored — the key to delete and the etag identifying it.
        still_unregistered: Returns whether no committed ``artifacts`` row references the key.
    """
    for obj in written:
        try:
            # The stat runs BEFORE the row probe so the probe is the last thing between the
            # decision and the delete: nothing but the delete call itself sits in that gap.
            head = await asyncio.to_thread(store.head, obj.key)
            if head is None:
                continue  # already gone; nothing left to reclaim
            if head.etag != obj.etag:
                _log.info(
                    "object %s was replaced after this attempt wrote it; leaving it alone",
                    obj.key,
                )
                continue
            if not await still_unregistered(obj.key):
                _log.info(
                    "object %s was registered by a peer attempt after the lock was released; "
                    "leaving it to that row",
                    obj.key,
                )
                continue
            await asyncio.to_thread(store.delete, obj.key)
        except CategorizedError:
            _log.warning(
                "deleting unregistered object %s failed; it has no artifacts row, so no "
                "reclaim sweep will reach it",
                obj.key,
                exc_info=True,
            )
