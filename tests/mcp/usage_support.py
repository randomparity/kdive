"""Helpers for tests that assert on the rows ``UsageTrackingMiddleware`` records.

``UsageTrackingMiddleware._record`` acquires its connection with a 1-second budget and
swallows every failure, so best-effort usage recording can never fail a tool call
(ADR-0148). A test that then asserts on the recorded row inherits both halves of that
contract: it must not let the acquire budget expire, and it must say so when one does.

Use :func:`open_warm_pool` for the pool and wrap the drive in
:func:`recording_must_not_fail` (#1527). ``DenialAuditMiddleware`` needs neither — it
acquires with the pool's 30-second default, so its ``audit_log`` rows are not on this
knife edge.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.middleware.usage import UsageTrackingMiddleware

_RECORDING_FAILURE = "usage recording failed"


class _Capture(logging.Handler):
    """Collect the middleware's recording-failure warnings, tracebacks included.

    ``_record`` logs its one warning with ``exc_info=True``, and the swallowed exception
    is the whole diagnostic value — ``record.getMessage()`` alone yields only the tool
    name. ``Formatter.format`` appends the traceback whenever ``exc_info`` is set, so it
    is what preserves the cause. Only this module's recording-failure warning is
    collected: an unrelated future warning on the same logger must not be reported as a
    recording failure.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.failures: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith(_RECORDING_FAILURE):
            self.failures.append(logging.Formatter().format(record))


@contextlib.contextmanager
def recording_must_not_fail() -> Iterator[None]:
    """Turn the middleware's swallowed recording failure into a named assertion.

    ``UsageTrackingMiddleware._record`` logs and swallows every failure so a bad
    recording can never fail the tool call. In a test that then asserts on the recorded
    row, a swallowed failure surfaces only as an empty result set — the bare
    ``IndexError`` reported in #1527, which says nothing about the cause. Re-raise every
    captured warning, traceback and all, instead.

    The check runs in the ``finally`` so a raising body cannot discard it; Python chains
    the original exception onto the ``AssertionError`` as its context.
    """
    logger = logging.getLogger(UsageTrackingMiddleware.__module__)
    handler = _Capture()
    previous = logger.level
    logger.setLevel(logging.WARNING)  # the logger's own level, not the global disable floor
    logger.addHandler(handler)
    # `logging.disable` is a process-global floor `setLevel` cannot lift, and the suite
    # treats it as mutable (tests/conftest.py snapshots it). Without this check a raised
    # floor would silently disarm the guard and #1527 would revert to a bare IndexError.
    assert logger.isEnabledFor(logging.WARNING), "recording-failure guard is disarmed"
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
        if handler.failures:
            raise AssertionError("\n\n".join(handler.failures))


async def open_warm_pool(url: str) -> AsyncConnectionPool:
    """A pool with its connection already established.

    ``open()`` defaults to ``wait=False``, which returns before any connection exists and
    leaves the first connect to a background task. The middleware then acquires with a
    1-second budget (its production default), so on a machine saturated by the suite's
    ``-n auto --dist worksteal`` run that first connect can exceed the budget, raise
    ``PoolTimeout``, and be swallowed — leaving no row (#1527). Waiting here moves the
    connect outside the budget, and makes a genuinely unreachable backend fail loudly at
    open time rather than as a silently missing row.
    """
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open(wait=True)
    return pool
