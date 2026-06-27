"""Production probe-adapter tests for the ephemeral build-host agent check (ADR-0167).

`_blocking_probe` classification is driven by an injected fake session factory (no libvirt, no
DB): a yielded transport means the agent connected, so a failure in the body (rc!=0 / exec drop)
is AGENT_UNREACHABLE while a CategorizedError escaping the session is HOST_UNREACHABLE unless its
category is PROVISIONING_FAILURE (agent never connected). The DB-backed test exercises host
enumeration, the no-staged-image skip, and the single-flight in-flight path over migrated Postgres.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

import libvirt
from psycopg_pool import AsyncConnectionPool

from kdive.db.build_hosts import BuildHost, BuildHostKind, BuildHostState
from kdive.diagnostics.buildhost_agent_check import BuildHostAgentOutcome
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.build_transport import CommandResult
from kdive.providers.remote_libvirt.diagnostics import buildhost_agent as adapter
from kdive.providers.remote_libvirt.lifecycle.readiness import wait_for_agent_responsive
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import libvirt_error

_RUN = UUID("00000000-0000-0000-0000-0000000000aa")


def _host(*, name: str = "eph", base_image_volume: str | None = "base.qcow2") -> BuildHost:
    return BuildHost(
        id=uuid4(),
        name=name,
        kind=BuildHostKind.EPHEMERAL_LIBVIRT,
        address=None,
        ssh_credential_ref=None,
        base_image_volume=base_image_volume,
        workspace_root="/build",
        max_concurrent=1,
        enabled=True,
        state=BuildHostState.READY,
        toolchain_desc=None,
    )


class _FakeTransport:
    def __init__(self, *, rc: int = 0, exec_raises: CategorizedError | None = None) -> None:
        self._rc = rc
        self._exec_raises = exec_raises

    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        if self._exec_raises is not None:
            raise self._exec_raises
        return CommandResult(returncode=self._rc, stdout="", stderr="")


def _session_factory(
    *,
    enter_raises: Exception | None = None,
    rc: int = 0,
    exec_raises: CategorizedError | None = None,
):
    @contextmanager
    def factory(
        base_image_volume: str,
        secret_registry: SecretRegistry,
        *,
        run_id: UUID,
        resource_name: str = "",
        source: object | None = None,
        wait_network: bool = True,
    ) -> Iterator[_FakeTransport]:
        if enter_raises is not None:
            raise enter_raises
        yield _FakeTransport(rc=rc, exec_raises=exec_raises)

    return factory


def _blocking(host: BuildHost, factory) -> tuple[BuildHostAgentOutcome, bool]:
    return adapter._blocking_probe(host, _RUN, SecretRegistry(), factory)


def test_agent_ready_when_session_yields_and_rc_zero() -> None:
    outcome, transport_err = _blocking(_host(), _session_factory(rc=0))
    assert outcome is BuildHostAgentOutcome.AGENT_READY
    assert transport_err is False


def test_agent_unreachable_when_provisioning_failure_escapes() -> None:
    err = CategorizedError("agent never connected", category=ErrorCategory.PROVISIONING_FAILURE)
    outcome, _ = _blocking(_host(), _session_factory(enter_raises=err))
    assert outcome is BuildHostAgentOutcome.AGENT_UNREACHABLE


def test_agent_unreachable_when_trivial_command_rc_nonzero() -> None:
    outcome, _ = _blocking(_host(), _session_factory(rc=1))
    assert outcome is BuildHostAgentOutcome.AGENT_UNREACHABLE


def test_agent_unreachable_when_exec_drops_after_agent_connected() -> None:
    drop = CategorizedError("agent dropped", category=ErrorCategory.TRANSPORT_FAILURE)
    outcome, _ = _blocking(_host(), _session_factory(exec_raises=drop))
    assert outcome is BuildHostAgentOutcome.AGENT_UNREACHABLE


def test_host_unreachable_on_configuration_error_before_agent() -> None:
    err = CategorizedError("no pool", category=ErrorCategory.CONFIGURATION_ERROR)
    outcome, transport_err = _blocking(_host(), _session_factory(enter_raises=err))
    assert outcome is BuildHostAgentOutcome.HOST_UNREACHABLE
    assert transport_err is False


def _gate_driven_unresponsive_factory():
    """A session factory that drives the REAL wait_for_agent_responsive gate to its deadline.

    Using the production gate (not a hand-built error) means the raised CategorizedError carries
    the production agent_readiness marker, so a drift in the shared constant breaks this test.
    """

    @contextmanager
    def factory(
        base_image_volume: str,
        secret_registry: SecretRegistry,
        *,
        run_id: UUID,
        resource_name: str = "",
        source: object | None = None,
        wait_network: bool = True,
    ) -> Iterator[_FakeTransport]:
        def _always_unresponsive(domain: object, command: str, timeout: int, flags: int) -> str:
            raise libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE)

        ticks = iter(range(0, 1000))
        wait_for_agent_responsive(
            _always_unresponsive,
            object(),
            "eph",
            monotonic=lambda: float(next(ticks)),
            sleep=lambda _s: None,
            timeout_s=2.0,
            poll_s=1.0,
        )
        yield _FakeTransport()  # unreachable: the gate raises before yielding

    return factory


def test_agent_unreachable_when_agent_never_responsive() -> None:
    # The build session's guest-ping gate (ADR-0168) fails an agent that opens the channel but
    # never answers. That marked CONFIGURATION_ERROR is an agent (image) FAIL, not a host ERROR.
    outcome, _ = _blocking(_host(), _gate_driven_unresponsive_factory())
    assert outcome is BuildHostAgentOutcome.AGENT_UNREACHABLE


def test_host_unreachable_transport_error_on_tls_failure_before_agent() -> None:
    err = CategorizedError("tls down", category=ErrorCategory.TRANSPORT_FAILURE)
    outcome, transport_err = _blocking(_host(), _session_factory(enter_raises=err))
    assert outcome is BuildHostAgentOutcome.HOST_UNREACHABLE
    assert transport_err is True


def test_host_without_base_image_is_host_unreachable_without_session() -> None:
    def _explode(*a, **k):
        raise AssertionError("session must not be entered for a host with no base image")

    outcome, _ = _blocking(_host(base_image_volume=None), _explode)
    assert outcome is BuildHostAgentOutcome.HOST_UNREACHABLE


# ---- DB-backed: enumeration + single-flight in-flight path ---------------------------


async def _seed(pool: AsyncConnectionPool, *, name: str, enabled: bool = True) -> UUID:
    host_id = uuid4()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO build_hosts (id, name, kind, workspace_root, max_concurrent, enabled, "
            "base_image_volume) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                host_id,
                name,
                BuildHostKind.EPHEMERAL_LIBVIRT.value,
                "/build",
                1,
                enabled,
                "base.qcow2",
            ),
        )
    return host_id


def test_probe_enumerates_only_enabled_ephemeral_hosts(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            await _seed(pool, name="on", enabled=True)
            await _seed(pool, name="off", enabled=False)
            probe = adapter.buildhost_agent_probe(pool, session_factory=_session_factory(rc=0))
            results = await probe()
            names = {r.host_name for r in results}
            assert names == {"on"}  # disabled host skipped; seeded worker-local (local) skipped
            assert results[0].outcome is BuildHostAgentOutcome.AGENT_READY

    asyncio.run(_run())


def test_unexpected_exception_is_per_host_unreachable(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            await _seed(pool, name="flaky", enabled=True)
            # A plain (non-CategorizedError) failure from the session seam.
            boom = _session_factory(enter_raises=RuntimeError("unexpected error"))
            probe = adapter.buildhost_agent_probe(pool, session_factory=boom)
            results = await probe()
            # One host's unexpected failure is that host's HOST_UNREACHABLE — the aggregate is not
            # collapsed to a whole-check error (other hosts' verdicts survive).
            assert [r.outcome for r in results] == [BuildHostAgentOutcome.HOST_UNREACHABLE]

    asyncio.run(_run())


def test_probe_in_flight_marker_reports_host_unreachable(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            host_id = await _seed(pool, name="busy", enabled=True)
            # A live marker for the host (as a cross-process caller would hold) makes the probe's
            # own register hit the partial-unique index → ProbeInFlightError → HOST_UNREACHABLE.
            from kdive.db import buildhost_agent_probes as probes

            await probes.register(pool, build_host_id=host_id, run_id=uuid4())
            probe = adapter.buildhost_agent_probe(pool, session_factory=_session_factory(rc=0))
            results = await probe()
            assert results[0].outcome is BuildHostAgentOutcome.HOST_UNREACHABLE

    asyncio.run(_run())
