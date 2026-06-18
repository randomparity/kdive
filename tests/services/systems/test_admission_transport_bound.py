"""Transport-bound admission tests (#452, ADR-0126).

The synchronous provider ``rootfs_validator`` is the one blocking call on the
``systems.provision`` pre-mutation path. These tests pin (1) it is offloaded so a slow
validator does not stall a concurrent request, (2) a validator that blocks past the
pre-mutation bound returns a ``transport_failure`` envelope (not a dropped socket) with
no System/job written, and (3) the deadline is disabled at the first mutation so the
mutation segment runs unbounded.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from types import TracebackType
from typing import Any, LiteralString
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.tools.lifecycle.systems.provision import _admission_response
from kdive.profiles.provisioning import RootfsSource
from kdive.services.systems.admission import (
    AdmissionFailure,
    CreateSystemRequest,
    ProvisionJobAdmitted,
    SystemAdmission,
)
from tests.mcp.systems_support import (
    TEST_COMPONENT_SOURCES as _TEST_COMPONENT_SOURCES,
)
from tests.mcp.systems_support import (
    TEST_PROFILE_POLICY as _TEST_PROFILE_POLICY,
)
from tests.mcp.systems_support import (
    ctx as _ctx,
)
from tests.mcp.systems_support import (
    granted_allocation as _granted_allocation,
)
from tests.mcp.systems_support import (
    pool as _pool,
)
from tests.mcp.systems_support import (
    provisioning_profile as _profile,
)


def _admission(
    rootfs_validator: Callable[[RootfsSource], None],
    *,
    premutation_timeout_s: float | None = None,
    timeout_factory: Callable[[float], Any] | None = None,
) -> SystemAdmission:
    return SystemAdmission(
        _TEST_PROFILE_POLICY,
        _TEST_COMPONENT_SOURCES,
        rootfs_validator,
        premutation_timeout_s=premutation_timeout_s,
        timeout_factory=timeout_factory,
    )


async def _create(
    admission: SystemAdmission, pool: AsyncConnectionPool, alloc_id: str
) -> AdmissionFailure | ProvisionJobAdmitted:
    result = await admission.create_for_allocation(
        pool,
        _ctx(),
        CreateSystemRequest(allocation_id=UUID(alloc_id), profile=_profile(), mode="provision"),
    )
    assert isinstance(result, AdmissionFailure | ProvisionJobAdmitted)
    return result


async def _count(pool: AsyncConnectionPool, table: str) -> int:
    queries: dict[str, LiteralString] = {
        "systems": "SELECT count(*) AS n FROM systems",
        "jobs": "SELECT count(*) AS n FROM jobs",
    }
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(queries[table])
        row = await cur.fetchone()
    assert row is not None
    return int(row["n"])


def test_slow_validator_does_not_stall_a_concurrent_request(migrated_url: str) -> None:
    """Acceptance #2: the blocking validator runs off the event loop (asyncio.to_thread)."""
    entered = threading.Event()
    release = threading.Event()
    invoked: list[RootfsSource] = []

    def _blocking_validator(rootfs: RootfsSource) -> None:
        invoked.append(rootfs)
        entered.set()
        if not release.wait(timeout=10):
            raise AssertionError("validator was never released")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            admission = _admission(_blocking_validator, premutation_timeout_s=30.0)
            task = asyncio.create_task(_create(admission, pool, alloc_id))
            # Wait until the provision is genuinely blocked inside the validator.
            await asyncio.to_thread(entered.wait, 10)

            concurrent_ran = False

            async def _concurrent() -> None:
                nonlocal concurrent_ran
                concurrent_ran = True

            # With the offload, the loop is free and this completes promptly. Without it,
            # the validator runs inline and starves this coroutine -> TimeoutError.
            await asyncio.wait_for(_concurrent(), timeout=5)
            assert concurrent_ran is True

            release.set()
            result = await task
        assert isinstance(result, ProvisionJobAdmitted)
        assert invoked  # the validator actually ran (not skipped)

    asyncio.run(_run())


def test_premutation_bound_fires_returns_transport_failure_no_writes(migrated_url: str) -> None:
    """Acceptance #1: a validator blocking past the bound -> transport_failure, no writes."""
    release = threading.Event()

    def _wedged_validator(_rootfs: RootfsSource) -> None:
        release.wait(timeout=10)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            admission = _admission(_wedged_validator, premutation_timeout_s=0.05)
            result = await _create(admission, pool, alloc_id)
            systems_n = await _count(pool, "systems")
            jobs_n = await _count(pool, "jobs")
        release.set()
        assert isinstance(result, AdmissionFailure)
        assert result.category is ErrorCategory.TRANSPORT_FAILURE
        assert result.failure_message is not None
        assert not hasattr(result, "detail")
        assert not hasattr(result, "suggested_next_actions")
        assert not hasattr(result, "data")
        assert systems_n == 0
        assert jobs_n == 0

        envelope = _admission_response(result)
        assert envelope.status == "error"
        assert envelope.error_category == "transport_failure"
        assert envelope.retryable is True
        assert envelope.detail
        assert envelope.suggested_next_actions == ["systems.provision"]

    asyncio.run(_run())


class _SpyTimeout:
    """Records reschedule() calls and delegates the deadline to a real asyncio.timeout."""

    def __init__(self, when: float, recorder: list[float | None]) -> None:
        self._inner = asyncio.timeout(when)
        self._recorder = recorder

    async def __aenter__(self) -> _SpyTimeout:
        await self._inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        return await self._inner.__aexit__(exc_type, exc_val, exc_tb)

    def reschedule(self, when: float | None) -> None:
        self._recorder.append(when)
        self._inner.reschedule(when)


def test_first_mutation_disables_the_deadline(migrated_url: str) -> None:
    """Acceptance #4: reschedule(None) fires once, before the first mutation lands."""
    reschedules: list[float | None] = []

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            admission = _admission(
                lambda _rootfs: None,
                premutation_timeout_s=30.0,
                timeout_factory=lambda when: _SpyTimeout(when, reschedules),
            )
            result = await _create(admission, pool, alloc_id)
        assert isinstance(result, ProvisionJobAdmitted)
        # Exactly one disable, and it disables (None), on the mutating path.
        assert reschedules == [None]

    asyncio.run(_run())


def test_no_mutation_branch_never_disables_the_deadline(migrated_url: str) -> None:
    """Acceptance #4 corollary: a terminal-existing-System failure disables zero times."""
    from kdive.domain.capacity.state import SystemState
    from tests.mcp.lifecycle.test_systems_tools import _seed_system

    reschedules: list[float | None] = []

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _seed_system(pool, alloc_id, SystemState.TORN_DOWN)
            admission = _admission(
                lambda _rootfs: None,
                premutation_timeout_s=30.0,
                timeout_factory=lambda when: _SpyTimeout(when, reschedules),
            )
            result = await _create(admission, pool, alloc_id)
        assert isinstance(result, AdmissionFailure)
        assert reschedules == []

    asyncio.run(_run())
