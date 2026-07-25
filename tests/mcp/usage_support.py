"""Helpers for tests that assert on rows a best-effort middleware writes.

``UsageTrackingMiddleware._record`` and ``DenialAuditMiddleware``'s audit write both
log-and-swallow every failure, so a bad write can never fail the tool call (ADR-0148).
A test that then asserts on the written row inherits both halves of that contract: it
must not provoke a failure, and it must say so when one happens anyway.

Use :func:`warm_pool` for the pool and wrap the drive in :func:`recording_must_not_fail`
(and :func:`denial_audit_must_not_fail` where ``audit_log`` rows are asserted) — see
#1527. Only the usage write was ever on the ``PoolTimeout`` knife edge that #1527 is
about: it acquires with a 1-second budget, where the denial audit uses the pool's
30-second default. The denial guard is here because that except clause swallows more
than timeouts — a constraint violation or a broken connection lands there too, and
produces the same uninformative empty-rows assertion.

Nesting matters at the call sites: ``recording_must_not_fail()`` must be the OUTER
manager when it is paired with ``contextlib.suppress(Exception)``. Suppress on the
outside would swallow the guard's own ``AssertionError`` and silently make it a no-op.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Iterator

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.middleware.denial_audit import DenialAuditMiddleware
from kdive.mcp.middleware.usage import UsageTrackingMiddleware


class _Capture(logging.Handler):
    """Collect one module's swallowed-write warnings, tracebacks included.

    Both middlewares log their one warning with ``exc_info=True``, and the swallowed
    exception is the whole diagnostic value — ``record.getMessage()`` alone yields only
    the tool name. ``Formatter.format`` appends the traceback whenever ``exc_info`` is
    set, so it is what preserves the cause. Matching on ``prefix`` keeps an unrelated
    future warning on the same logger from being reported as a lost row.
    """

    def __init__(self, prefix: str) -> None:
        super().__init__(level=logging.WARNING)
        self._prefix = prefix
        self.failures: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith(self._prefix):
            self.failures.append(logging.Formatter().format(record))


@contextlib.contextmanager
def _write_must_not_fail(logger_name: str, prefix: str) -> Iterator[None]:
    """Turn a middleware's swallowed write failure into a named assertion.

    A swallowed failure surfaces only as an empty result set — the bare ``IndexError``
    reported in #1527, which says nothing about the cause. Re-raise every captured
    warning, traceback and all, instead.

    The check runs in the ``finally`` so a raising body cannot discard it; Python chains
    the original exception onto the ``AssertionError`` as its context.
    """
    logger = logging.getLogger(logger_name)
    handler = _Capture(prefix)
    previous = logger.level
    logger.setLevel(logging.WARNING)  # the logger's own level, not the global disable floor
    logger.addHandler(handler)
    try:
        # `logging.disable` is a process-global floor that `setLevel` cannot lift, so a
        # raised floor would leave this guard capturing nothing and #1527 would revert to
        # a bare IndexError with no sign the guard was blind. Nothing in the repo raises
        # it today; this asserts that stays true. Inside the `try` so the disarm path
        # still restores the logger.
        assert logger.isEnabledFor(logging.WARNING), f"{logger_name} guard is disarmed"
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
        if handler.failures:
            raise AssertionError("\n\n".join(handler.failures))


def recording_must_not_fail() -> contextlib.AbstractContextManager[None]:
    """Guard the ``tool_invocation`` write ``UsageTrackingMiddleware`` swallows."""
    return _write_must_not_fail(UsageTrackingMiddleware.__module__, "usage recording failed")


def denial_audit_must_not_fail() -> contextlib.AbstractContextManager[None]:
    """Guard the ``audit_log`` write ``DenialAuditMiddleware`` swallows."""
    return _write_must_not_fail(DenialAuditMiddleware.__module__, "failed to audit RoleDenied")


@contextlib.asynccontextmanager
async def warm_pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    """A pool whose connection is established before the caller gets it.

    ``open()`` defaults to ``wait=False``, which returns before any connection exists and
    leaves the first connect to a background task. ``UsageTrackingMiddleware`` then
    acquires with a 1-second budget (its production default), so on a machine saturated
    by the suite's ``-n auto --dist worksteal`` run that first connect can exceed the
    budget, raise ``PoolTimeout``, and be swallowed — leaving no row (#1527). Waiting here
    moves the connect outside the budget, and makes a genuinely unreachable backend fail
    loudly at open time rather than as a silently missing row.

    A context manager rather than a plain factory so closure is structural: a caller
    cannot hold an open pool — and its background workers — without closing it. Sized for
    the strictly sequential acquires its current callers make; a consumer needing three
    at once should raise ``max_size`` rather than queue behind it.
    """
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open(wait=True)
    try:
        # Carried here rather than in one standalone test so that every consumer inherits
        # the regression pin: a revert to the cold `open()` reddens each caller, not just
        # the test that happens to name the contract.
        assert pool.get_stats()["pool_available"] >= 1, "pool opened cold — see #1527"
        yield pool
    finally:
        await pool.close()
