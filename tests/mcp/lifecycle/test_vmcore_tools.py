"""vmcore.* / postmortem.* tool + handler tests — handlers called directly."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import StoredArtifact
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.artifacts import vmcore as vmcore_plane
from kdive.jobs.handlers.console.capture_telemetry import CaptureTelemetry
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import Authorizing, CaptureVmcorePayload
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.lifecycle import vmcore as vmcore_tools
from kdive.mcp.tools.lifecycle import vmcore_handlers as vmcore_handler_tools
from kdive.providers.ports.retrieve import (
    CaptureOutput,
    CrashOutput,
    CrashPostmortem,
)
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp._seed import seed_crashed_system, seed_run_on_system
from tests.mcp.json_data import data_str
from tests.mcp.systems_support import provider_resolver

_AUTH = Authorizing(principal="u", agent_session="s", project="proj")
_TEST_CAPTURE_METHODS = frozenset({CaptureMethod.HOST_DUMP})


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="u", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _crashed_run(pool: AsyncConnectionPool, *, project: str = "proj") -> tuple[str, str]:
    """Seed a crashed System with a bound Run; return ``(system_id, run_id)`` (ADR-0244)."""
    sys_id = await seed_crashed_system(pool, project=project)
    run_id = await seed_run_on_system(
        pool, sys_id, debuginfo_ref=None, build_id=None, project=project
    )
    return sys_id, run_id


async def _fetch_vmcore(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    method: str = "host_dump",
    idempotency_key: str | None = None,
):
    return await _vmcore_handlers().fetch_vmcore(
        pool,
        ctx,
        run_id=run_id,
        method=method,
        idempotency_key=idempotency_key,
    )


def _capture_output(run_id: str, method: CaptureMethod = CaptureMethod.HOST_DUMP) -> CaptureOutput:
    raw = StoredArtifact(
        f"local/runs/{run_id}/vmcore-{method.value}", "e1", Sensitivity.SENSITIVE, "vmcore"
    )
    red = StoredArtifact(
        f"local/runs/{run_id}/vmcore-{method.value}-redacted",
        "e2",
        Sensitivity.REDACTED,
        "vmcore",
    )
    return CaptureOutput(raw=raw, redacted=red, vmcore_build_id="deadbeef", raw_size_bytes=512)


class _FakeRetriever:
    """Records capture calls; returns a canned CaptureOutput or raises a planted error."""

    def __init__(self, run_id: str, *, raises: CategorizedError | None = None) -> None:
        self._run_id = run_id
        self._raises = raises
        self.calls = 0
        self.methods: list[CaptureMethod] = []

    def capture(self, system_id: UUID, run_id: UUID, method: CaptureMethod) -> CaptureOutput:
        self.calls += 1
        self.methods.append(method)
        if self._raises is not None:
            raise self._raises
        return _capture_output(self._run_id, method)


class _NoCaptureRetriever:
    """Fails the test if .capture is ever called (idempotency probe)."""

    def capture(self, system_id: UUID, run_id: UUID, method: CaptureMethod) -> CaptureOutput:
        raise AssertionError("capture must not be called when a vmcore row already exists")


class _FakeCrash:
    """Records postmortem kwargs; returns a canned CrashOutput with a planted secret."""

    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def run_crash_postmortem(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str, commands: list[str]
    ) -> CrashOutput:
        self.kwargs = {
            "vmcore_ref": vmcore_ref,
            "debuginfo_ref": debuginfo_ref,
            "expected_build_id": expected_build_id,
            "commands": commands,
        }
        return CrashOutput(
            results={c: {"ran": True} for c in commands},
            transcript="$ log\npassword=hunter2\nok",
            truncated=False,
        )


def _vmcore_handlers(crash: CrashPostmortem | None = None) -> vmcore_handler_tools.VmcoreHandlers:
    return vmcore_handler_tools.VmcoreHandlers(
        resolver=provider_resolver(
            crash_postmortem=crash or _FakeCrash(),
            supported_capture_methods=_TEST_CAPTURE_METHODS,
        ),
        secret_registry=SecretRegistry(),
    )


class _TruncatingCrash:
    """A CrashPostmortem whose output is byte-capped (truncated=True)."""

    def run_crash_postmortem(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str, commands: list[str]
    ) -> CrashOutput:
        return CrashOutput(
            results={c: {"ran": True} for c in commands},
            transcript="capped output",
            truncated=True,
        )


class _RaisingCrash:
    """A CrashPostmortem that raises a planted CategorizedError."""

    def __init__(self, category: ErrorCategory) -> None:
        self._category = category

    def run_crash_postmortem(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str, commands: list[str]
    ) -> CrashOutput:
        raise CategorizedError("planted", category=self._category)


# --- vmcore.fetch tool ---------------------------------------------------------------------


def _real_local_handlers() -> vmcore_handler_tools.VmcoreHandlers:
    """A VmcoreHandlers bound to the REAL local-libvirt runtime (descriptor narrowing applies).

    Unlike ``_vmcore_handlers``, this does not override ``supported_capture_methods`` — it uses
    the production local composition, so the ADR-0208 narrowing to ``{KDUMP}`` is what drives the
    admission decision.
    """
    from kdive.domain.catalog.resources import ResourceKind
    from kdive.providers.assembly.composition import build_local_runtime
    from kdive.providers.core.resolver import ProviderResolver

    registry = SecretRegistry()
    runtime = build_local_runtime(secret_registry=registry)
    resolver = ProviderResolver({ResourceKind.LOCAL_LIBVIRT: runtime})
    return vmcore_handler_tools.VmcoreHandlers(resolver=resolver, secret_registry=registry)


async def _job_count(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'capture_vmcore'")
        row = await cur.fetchone()
    assert row is not None
    return int(row["n"])


def test_fetch_vmcore_host_dump_admitted_on_local_after_b4(migrated_url: str) -> None:
    # B4 (ADR-0211) wired local's HOST_DUMP seam and ADR-0208/0211 added it to local's advertised
    # set, so an explicit vmcore.fetch(host_dump) is now ADMITTED (no longer the A1 fail-fast
    # capability_unsupported rejection): it enqueues one capture_vmcore job dedup'd under the
    # method-encoded key. Driven through the REAL local runtime so the descriptor drives the
    # decision.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            handlers = _real_local_handlers()
            resp = await handlers.fetch_vmcore(pool, _ctx(), run_id=run_id, method="host_dump")
            assert resp.status == "queued"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind = 'capture_vmcore' "
                    "AND dedup_key = %s",
                    (f"{run_id}:capture_vmcore:host_dump",),
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_fetch_vmcore_no_method_resolves_kdump_on_crashkernel_local(migrated_url: str) -> None:
    # ADR-0209: with no method, vmcore.fetch resolves capture_method(profile) clamped to the
    # core-producing set. The seeded crashkernel local System resolves to KDUMP, which local
    # supports, so the no-method call is admitted and dedups under the resolved :kdump key.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            handlers = _real_local_handlers()
            resp = await handlers.fetch_vmcore(pool, _ctx(), run_id=run_id, method=None)
            assert resp.status == "queued"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind = 'capture_vmcore' "
                    "AND dedup_key = %s",
                    (f"{run_id}:capture_vmcore:kdump",),
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


async def _set_console_only_profile(pool: AsyncConnectionPool, sys_id: str) -> None:
    """Rewrite the System's profile so capture_method(profile) resolves to non-core CONSOLE."""
    from psycopg.types.json import Jsonb

    profile = {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 4,
        "memory_mb": 4096,
        "disk_gb": 20,
        "boot_method": "direct-kernel",
        "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
        "provider": {
            "local-libvirt": {
                "domain_xml_params": {"machine": "q35"},
                "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/fedora-40.qcow2"},
            }
        },
    }
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE systems SET provisioning_profile = %s WHERE id = %s", (Jsonb(profile), sys_id)
        )


def test_fetch_vmcore_no_method_console_only_requires_explicit_method(migrated_url: str) -> None:
    # ADR-0209: a console-only System has no implicit core capture method, so the no-method call
    # is a configuration_error (missing_required_field) — NOT capability_unsupported, since the
    # provider does support core methods; the caller simply omitted one. No job is enqueued.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            await _set_console_only_profile(pool, sys_id)
            handlers = _real_local_handlers()
            resp = await handlers.fetch_vmcore(pool, _ctx(), run_id=run_id, method=None)
            jobs = await _job_count(pool)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert data_str(resp, "reason") == "missing_required_field"
        assert jobs == 0

    asyncio.run(_run())


def test_fetch_vmcore_kdump_admitted_on_local(migrated_url: str) -> None:
    # The narrowing leaves the one method local CAN fetch (KDUMP) admissible.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            handlers = _real_local_handlers()
            resp = await handlers.fetch_vmcore(pool, _ctx(), run_id=run_id, method="kdump")
            jobs = await _job_count(pool)
        assert resp.status == "queued"
        assert jobs == 1

    asyncio.run(_run())


_CRASH_GATE_FULL = frozenset(
    {
        "KEXEC_CORE",
        "KEXEC",
        "CRASH_DUMP",
        "PROC_VMCORE",
        "VMCORE_INFO",
        "FW_CFG_SYSFS",
        "RELOCATABLE",
    }
)


def test_fetch_vmcore_kdump_refused_when_config_lacks_crash_symbols(migrated_url: str) -> None:
    # ADR-0318: a KDUMP vmcore on a kernel whose uploaded config provably lacks a crash symbol is
    # refused with a categorized, symbol-naming reason and enqueues no job.
    from unittest.mock import patch

    from kdive.kernel_config.parse import KernelConfig

    missing = KernelConfig(_CRASH_GATE_FULL - {"KEXEC_CORE"})

    async def _fake_load(conn: Any, run_id: Any, *, store_factory: Any = None) -> KernelConfig:
        return missing

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            handlers = _real_local_handlers()
            with patch("kdive.kernel_config.gate.load_effective_config", _fake_load):
                resp = await handlers.fetch_vmcore(pool, _ctx(), run_id=run_id, method="kdump")
            jobs = await _job_count(pool)
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "kernel_missing_crash_config"
        assert "KEXEC_CORE" in cast(list[str], resp.data["missing"])
        assert jobs == 0  # refused before enqueue

    asyncio.run(_run())


def test_fetch_vmcore_host_dump_ungated_even_with_unsupported_config(migrated_url: str) -> None:
    # host_dump is host-side (QEMU dump-guest-memory), so it needs no guest kernel config: the gate
    # never fires for it even when the uploaded config would fail the kdump gate.
    from unittest.mock import patch

    from kdive.kernel_config.parse import KernelConfig

    empty = KernelConfig(frozenset())

    async def _fake_load(conn: Any, run_id: Any, *, store_factory: Any = None) -> KernelConfig:
        return empty

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            handlers = _real_local_handlers()
            with patch("kdive.kernel_config.gate.load_effective_config", _fake_load):
                resp = await handlers.fetch_vmcore(pool, _ctx(), run_id=run_id, method="host_dump")
        assert resp.status == "queued"  # host_dump path never gates

    asyncio.run(_run())


def test_fetch_vmcore_crashed_enqueues_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            resp = await _fetch_vmcore(pool, _ctx(), run_id=run_id)
            assert resp.status == "queued"
            assert resp.data["run_id"] == run_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind = 'capture_vmcore' "
                    "AND dedup_key = %s",
                    (f"{run_id}:capture_vmcore:host_dump",),
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_fetch_vmcore_keyed_retry_replays_one_job(migrated_url: str) -> None:
    """A repeated idempotency_key returns the identical envelope and enqueues one job."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            first = await _fetch_vmcore(pool, _ctx(), run_id=run_id, idempotency_key="k1")
            second = await _fetch_vmcore(pool, _ctx(), run_id=run_id, idempotency_key="k1")
            assert first.model_dump() == second.model_dump()
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind = 'capture_vmcore' "
                    "AND dedup_key = %s",
                    (f"{run_id}:capture_vmcore:host_dump",),
                )
                jobs = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM idempotency_keys WHERE kind = 'vmcore.fetch'"
                )
                keys = await cur.fetchone()
        assert jobs is not None and jobs["n"] == 1
        assert keys is not None and keys["n"] == 1

    asyncio.run(_run())


def test_fetch_vmcore_oversized_key_is_config_error(migrated_url: str) -> None:
    """An over-long idempotency_key fails closed before any enqueue."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            resp = await _fetch_vmcore(pool, _ctx(), run_id=run_id, idempotency_key="x" * 201)
            assert resp.status == "error"
            assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'capture_vmcore'")
                row = await cur.fetchone()
        assert row is not None and row["n"] == 0

    asyncio.run(_run())


def test_fetch_vmcore_non_crashed_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE systems SET state = 'torn_down' WHERE id = %s", (sys_id,)
                )
            resp = await _fetch_vmcore(pool, _ctx(), run_id=run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "torn_down"
        # The detail names why the state blocks capture (was null before #734).
        assert resp.detail is not None
        assert "CRASHED" in resp.detail
        assert "torn_down" in resp.detail

    asyncio.run(_run())


def test_fetch_vmcore_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            with pytest.raises(AuthorizationError):
                await _fetch_vmcore(pool, _ctx(Role.VIEWER), run_id=run_id)

    asyncio.run(_run())


def test_fetch_vmcore_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _fetch_vmcore(pool, _ctx(), run_id="nope")
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_uuid"
        assert resp.detail is not None and "nope" in resp.detail

    asyncio.run(_run())


def test_fetch_rejects_unsupported_method(migrated_url: str) -> None:
    # The fake runtime supports only {HOST_DUMP}; an explicit kdump is capability_unsupported.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            resp = await _fetch_vmcore(pool, _ctx(), run_id=run_id, method="kdump")
            jobs = await _job_count(pool)
            assert resp.status == "error"
            assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
            assert data_str(resp, "reason") == "capability_unsupported"
            assert data_str(resp, "capability") == "capture_method:kdump"
            assert resp.data["supported"] == ["host_dump"]
            assert jobs == 0

    asyncio.run(_run())


def test_fetch_rejects_non_core_method(migrated_url: str) -> None:
    # A non-core method (console) is rejected before the capability check — it produces no vmcore.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            resp = await _fetch_vmcore(pool, _ctx(), run_id=run_id, method="console")
            jobs = await _job_count(pool)
            assert resp.status == "error"
            assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
            assert data_str(resp, "reason") == "method does not produce a vmcore"
            assert jobs == 0

    asyncio.run(_run())


def test_fetch_records_method_in_dedup_key(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            resp = await _fetch_vmcore(pool, _ctx(), run_id=run_id, method="host_dump")
            assert resp.status == "queued"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE dedup_key = %s",
                    (f"{run_id}:capture_vmcore:host_dump",),
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_fetch_vmcore_unbound_run_is_config_error(migrated_url: str) -> None:
    # A Run not bound to a System (run.system_id is None) has no capture target, so vmcore.fetch
    # fails closed with a configuration_error (run_unbound) and enqueues no job (ADR-0244).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            async with pool.connection() as conn:
                await conn.execute("UPDATE runs SET system_id = NULL WHERE id = %s", (run_id,))
            resp = await _fetch_vmcore(pool, _ctx(), run_id=run_id)
            jobs = await _job_count(pool)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "run_unbound"
        assert jobs == 0

    asyncio.run(_run())


# --- capture handler -----------------------------------------------------------------------


async def _enqueue_capture(
    pool: AsyncConnectionPool, run_id: str, method: str = "host_dump"
) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.CAPTURE_VMCORE,
            CaptureVmcorePayload(run_id=run_id, method=CaptureMethod(method)),
            _AUTH,
            f"{run_id}:capture_vmcore:{method}",
        )


async def _artifact_count(pool: AsyncConnectionPool, run_id: str) -> int:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT count(*) AS n FROM artifacts WHERE owner_kind = 'runs' AND owner_id = %s",
            (run_id,),
        )
        row = await cur.fetchone()
    return 0 if row is None else int(row["n"])


def test_capture_handler_stores_rows_and_returns_ref(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            job = await _enqueue_capture(pool, run_id)
            retriever = _FakeRetriever(run_id)
            async with pool.connection() as conn:
                ref = await vmcore_plane.capture_handler(
                    conn, job, resolver=provider_resolver(retriever=retriever)
                )
            assert ref == f"local/runs/{run_id}/vmcore-host_dump"
            assert retriever.calls == 1
            assert await _artifact_count(pool, run_id) == 2
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT sensitivity FROM artifacts WHERE owner_kind = 'runs' "
                    "AND owner_id = %s ORDER BY sensitivity",
                    (run_id,),
                )
                rows = await cur.fetchall()
        assert [r["sensitivity"] for r in rows] == ["redacted", "sensitive"]

    asyncio.run(_run())


def test_capture_handler_plumbs_method_to_retriever(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            job = await _enqueue_capture(pool, run_id, method="host_dump")
            retriever = _FakeRetriever(run_id)
            async with pool.connection() as conn:
                await vmcore_plane.capture_handler(
                    conn, job, resolver=provider_resolver(retriever=retriever)
                )
        assert retriever.methods == [CaptureMethod.HOST_DUMP]

    asyncio.run(_run())


def test_capture_handler_idempotent_skips_recapture(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                    "retention_class) VALUES ('runs', %s, %s, 'e', 'sensitive', 'vmcore')",
                    (run_id, f"local/runs/{run_id}/vmcore-host_dump"),
                )
            job = await _enqueue_capture(pool, run_id)
            async with pool.connection() as conn:
                ref = await vmcore_plane.capture_handler(
                    conn, job, resolver=provider_resolver(retriever=_NoCaptureRetriever())
                )
            assert ref == f"local/runs/{run_id}/vmcore-host_dump"
            assert await _artifact_count(pool, run_id) == 1  # no second row

    asyncio.run(_run())


def test_capture_handler_rejects_different_method(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                    "retention_class) VALUES ('runs', %s, %s, 'e', 'sensitive', 'vmcore')",
                    (run_id, f"local/runs/{run_id}/vmcore-host_dump"),
                )
            job = await _enqueue_capture(pool, run_id, method="kdump")
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await vmcore_plane.capture_handler(
                        conn, job, resolver=provider_resolver(retriever=_NoCaptureRetriever())
                    )
            assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert exc.value.details["existing_method"] == "host_dump"
            assert exc.value.details["requested_method"] == "kdump"
            assert await _artifact_count(pool, run_id) == 1  # no second core written

    asyncio.run(_run())


def test_captured_method_rejects_bare_key() -> None:
    with pytest.raises(CategorizedError) as exc:
        vmcore_plane.captured_method("local/systems/x/vmcore")
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_capture_handler_no_core_raises_readiness(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            job = await _enqueue_capture(pool, run_id)
            err = CategorizedError("no core", category=ErrorCategory.READINESS_FAILURE)
            retriever = _FakeRetriever(run_id, raises=err)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await vmcore_plane.capture_handler(
                        conn, job, resolver=provider_resolver(retriever=retriever)
                    )
            assert exc.value.category is ErrorCategory.READINESS_FAILURE
            assert await _artifact_count(pool, run_id) == 0

    asyncio.run(_run())


def test_capture_handler_missing_run_is_infra_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ghost = str(uuid4())
            job = await _enqueue_capture(pool, ghost)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await vmcore_plane.capture_handler(
                        conn, job, resolver=provider_resolver(retriever=_FakeRetriever(ghost))
                    )
        assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE

    asyncio.run(_run())


# --- vmcore.list ---------------------------------------------------------------------------


def test_list_vmcores_redacted_only(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            job = await _enqueue_capture(pool, run_id)
            async with pool.connection() as conn:
                await vmcore_plane.capture_handler(
                    conn, job, resolver=provider_resolver(retriever=_FakeRetriever(run_id))
                )
            resp = await vmcore_tools.list_vmcores(pool, _ctx(), run_id=run_id)
        keys = {r.refs["object"] for r in resp.items}
        assert keys == {f"local/runs/{run_id}/vmcore-host_dump-redacted"}

    asyncio.run(_run())


def test_list_vmcores_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            job = await _enqueue_capture(pool, run_id)
            async with pool.connection() as conn:
                await vmcore_plane.capture_handler(
                    conn, job, resolver=provider_resolver(retriever=_FakeRetriever(run_id))
                )
            with pytest.raises(AuthorizationError):
                await vmcore_tools.list_vmcores(pool, _ctx(role=None), run_id=run_id)

    asyncio.run(_run())


def test_list_vmcores_surfaces_run_owned_redacted_core(migrated_url: str) -> None:
    # ADR-0244 regression guard: a Run-owned redacted core must surface through the run-addressed
    # vmcore.list. Had vmcore.list stayed System-addressed it would have returned empty here — the
    # silent regression this test exists to catch.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            job = await _enqueue_capture(pool, run_id)
            async with pool.connection() as conn:
                await vmcore_plane.capture_handler(
                    conn, job, resolver=provider_resolver(retriever=_FakeRetriever(run_id))
                )
            resp = await vmcore_tools.list_vmcores(pool, _ctx(), run_id=run_id)
        assert len(resp.items) == 1
        assert resp.items[0].refs["object"] == f"local/runs/{run_id}/vmcore-host_dump-redacted"

    asyncio.run(_run())


# --- postmortem.crash ----------------------------------------------------------------------


async def _crashed_with_built_run(pool: AsyncConnectionPool) -> str:
    sys_id = await seed_crashed_system(pool)
    run_id = await seed_run_on_system(
        pool, sys_id, debuginfo_ref="k/runs/r/vmlinux", build_id="deadbeef"
    )
    job = await _enqueue_capture(pool, run_id)
    async with pool.connection() as conn:
        await vmcore_plane.capture_handler(
            conn, job, resolver=provider_resolver(retriever=_FakeRetriever(run_id))
        )
    return run_id


def test_postmortem_crash_bad_command_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _crashed_with_built_run(pool)
            crash = _FakeCrash()
            resp = await _vmcore_handlers(crash).postmortem_crash(
                pool, _ctx(), run_id=run_id, commands=["bt | sh"]
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert crash.kwargs == {}  # the port was never called

    asyncio.run(_run())


def test_postmortem_crash_runs_and_redacts(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _crashed_with_built_run(pool)
            crash = _FakeCrash()
            resp = await _vmcore_handlers(crash).postmortem_crash(
                pool, _ctx(), run_id=run_id, commands=["log"]
            )
        assert resp.status != "error"
        transcript = data_str(resp, "transcript")
        assert "hunter2" not in transcript
        assert "[REDACTED]" in transcript
        assert crash.kwargs["expected_build_id"] == "deadbeef"

    asyncio.run(_run())


def test_postmortem_crash_surfaces_truncated_flag(migrated_url: str) -> None:
    # A byte-capped transcript must signal `truncated` so the caller never reads a trimmed
    # transcript as complete (the cap lives in run_crash_postmortem).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _crashed_with_built_run(pool)
            resp = await _vmcore_handlers(_TruncatingCrash()).postmortem_crash(
                pool, _ctx(), run_id=run_id, commands=["log"]
            )
        assert resp.status != "error"
        assert resp.data["truncated"] is True

    asyncio.run(_run())


def test_postmortem_crash_not_truncated_when_small(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _crashed_with_built_run(pool)
            resp = await _vmcore_handlers(_FakeCrash()).postmortem_crash(
                pool, _ctx(), run_id=run_id, commands=["log"]
            )
        assert resp.status != "error"
        assert resp.data["truncated"] is False

    asyncio.run(_run())


def test_postmortem_crash_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _crashed_with_built_run(pool)
            with pytest.raises(AuthorizationError):
                await _vmcore_handlers().postmortem_crash(
                    pool, _ctx(role=None), run_id=run_id, commands=["log"]
                )

    asyncio.run(_run())


def test_postmortem_crash_unbuilt_run_is_not_found(migrated_url: str) -> None:
    # postmortem_crash shares resolve_run_vmcore_target with introspect.from_vmcore: a run with
    # no target artifact (null debuginfo / no build / no core) is not_found (ADR-0097). A
    # malformed command batch still stays configuration_error (a separate guard in the tool).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
            resp = await _vmcore_handlers().postmortem_crash(
                pool, _ctx(), run_id=run_id, commands=["log"]
            )
        assert resp.status == "error" and resp.error_category == "not_found"

    asyncio.run(_run())


def test_postmortem_crash_provenance_mismatch_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _crashed_with_built_run(pool)
            crash = _RaisingCrash(ErrorCategory.CONFIGURATION_ERROR)
            resp = await _vmcore_handlers(crash).postmortem_crash(
                pool, _ctx(), run_id=run_id, commands=["log"]
            )
        # The provider raises CategorizedError; the tool returns a typed failure, never a 500.
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_postmortem_triage_runs_and_relabels_actions(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _crashed_with_built_run(pool)
            crash = _FakeCrash()
            resp = await _vmcore_handlers(crash).postmortem_triage(pool, _ctx(), run_id=run_id)
        assert resp.status != "error"
        assert "hunter2" not in data_str(resp, "transcript")
        assert resp.suggested_next_actions == ["postmortem.triage", "artifacts.list"]
        assert crash.kwargs["commands"] == ["log", "bt"]  # the fixed triage batch

    asyncio.run(_run())


def test_postmortem_triage_never_booted_reports_no_vmcore(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
            resp = await _vmcore_handlers().postmortem_triage(pool, _ctx(), run_id=run_id)
        assert resp.status == "error" and resp.error_category == "not_found"
        # A never-booted run lacks debuginfo, build, AND a captured core. Triage is vmcore-centric,
        # so the operative gap (no_vmcore) surfaces first, not the earliest-unmet build precondition
        # (#553, ADR-0165). The next action points at the capture entry.
        assert resp.data["reason"] == "no_vmcore"
        assert resp.suggested_next_actions == ["vmcore.fetch", "runs.get"]
        # The non-console-crash no_vmcore envelope carries no expected_boot_failure key — pinning
        # the resolver's conditional attachment (safe_error_details forwards scalars; #734).
        assert "expected_boot_failure" not in resp.data

    asyncio.run(_run())


async def _console_crash_run_no_core(pool: AsyncConnectionPool) -> str:
    """A console_crash run with debuginfo + build but no captured core (early-boot crash)."""
    sys_id = await seed_crashed_system(pool)
    return await seed_run_on_system(
        pool,
        sys_id,
        debuginfo_ref="k/runs/r/vmlinux",
        build_id="deadbeef",
        expected_boot_failure={"kind": "console_crash", "pattern": "Kernel panic"},
    )


def test_console_crash_guidance_constant_pins_meaning() -> None:
    # The narrative is one shared constant; assert its stable substrings so a reworded constant
    # that drops the early-boot/console framing fails (#734, ADR-0227).
    assert "kexec" in vmcore_tools.CONSOLE_CRASH_GUIDANCE
    assert "console" in vmcore_tools.CONSOLE_CRASH_GUIDANCE


def test_postmortem_triage_console_crash_redirects_to_console(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _console_crash_run_no_core(pool)
            resp = await _vmcore_handlers().postmortem_triage(pool, _ctx(), run_id=run_id)
        # A console_crash run resolves to no vmcore by design (crash precedes kexec). Triage
        # redirects to the console with a non-suppressed configuration_error detail (#734).
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "expected_console_crash"
        assert resp.data["expected_boot_failure"] == "console_crash"
        assert resp.suggested_next_actions == ["runs.get", "artifacts.list"]
        assert resp.detail == vmcore_tools.CONSOLE_CRASH_GUIDANCE

    asyncio.run(_run())


def test_postmortem_crash_console_crash_redirects_to_console(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _console_crash_run_no_core(pool)
            resp = await _vmcore_handlers().postmortem_crash(
                pool, _ctx(), run_id=run_id, commands=["log"]
            )
        # postmortem.crash surfaces the same redirect (triage delegates to it; #734).
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "expected_console_crash"
        assert resp.suggested_next_actions == ["runs.get", "artifacts.list"]
        assert resp.detail == vmcore_tools.CONSOLE_CRASH_GUIDANCE

    asyncio.run(_run())


def test_postmortem_triage_console_crash_requires_viewer(migrated_url: str) -> None:
    # The redirect is reachable only through the caught precondition error; a non-viewer is
    # rejected by the resolver's AuthorizationError first, so the redirect never weakens authz.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _console_crash_run_no_core(pool)
            with pytest.raises(AuthorizationError):
                await _vmcore_handlers().postmortem_triage(pool, _ctx(role=None), run_id=run_id)

    asyncio.run(_run())


def test_postmortem_crash_no_core_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(
                pool, sys_id, debuginfo_ref="k/runs/r/vmlinux", build_id="deadbeef"
            )
            resp = await _vmcore_handlers().postmortem_crash(
                pool, _ctx(), run_id=run_id, commands=["log"]
            )
        assert resp.status == "error" and resp.error_category == "not_found"
        # A built run with no captured core names the no_vmcore precondition + next actions (#487).
        assert resp.data["reason"] == "no_vmcore"
        assert resp.suggested_next_actions == ["vmcore.fetch", "runs.get"]

    asyncio.run(_run())


# --- surface-wide redaction guard ----------------------------------------------------------


def test_no_raw_vmcore_key_in_any_read_response(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            job = await _enqueue_capture(pool, run_id)
            async with pool.connection() as conn:
                await vmcore_plane.capture_handler(
                    conn, job, resolver=provider_resolver(retriever=_FakeRetriever(run_id))
                )
            refs: list[str] = []
            from kdive.mcp.tools.catalog.artifacts.reads import artifacts_get, artifacts_list

            vmcores = await vmcore_tools.list_vmcores(pool, _ctx(), run_id=run_id)
            for r in vmcores.items:
                refs.extend(r.refs.values())
            listed = await artifacts_list(pool, _ctx(), system_id=sys_id)
            artifact_items = listed.items
            for r in artifact_items:
                refs.extend(r.refs.values())
            for r in artifact_items:
                got = await artifacts_get(pool, _ctx(), artifact_id=r.object_id)
                refs.extend(got.refs.values())
        assert refs  # something was returned
        # A raw core is `.../vmcore-{method}` (no `-redacted`); it must never surface.
        assert all(not ("/vmcore-" in key and not key.endswith("-redacted")) for key in refs)

    asyncio.run(_run())


# --- capture handler telemetry integration -------------------------------------------------


def _metric_points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    out: list[Any] = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


def test_capture_handler_emits_telemetry_on_success(migrated_url: str) -> None:
    """capture_handler must call telemetry.record with duration and bytes on success."""

    async def _run() -> None:
        reader = InMemoryMetricReader()
        meter = MeterProvider(metric_readers=[reader]).get_meter("test")
        telemetry = CaptureTelemetry(meter=meter)
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            job = await _enqueue_capture(pool, run_id)
            retriever = _FakeRetriever(run_id)
            async with pool.connection() as conn:
                await vmcore_plane.capture_handler(
                    conn,
                    job,
                    resolver=provider_resolver(retriever=retriever),
                    telemetry=telemetry,
                )
        dur_pts = _metric_points(reader, "kdive.vmcore.capture.duration")
        byte_pts = _metric_points(reader, "kdive.vmcore.capture.bytes")
        assert dur_pts, "duration not emitted on success"
        assert dur_pts[0].attributes["capture_method"] == CaptureMethod.HOST_DUMP.value
        assert dur_pts[0].attributes["outcome"] == "ok"
        assert byte_pts, "bytes not emitted on success"
        assert byte_pts[0].attributes["capture_method"] == CaptureMethod.HOST_DUMP.value
        expected_output = _capture_output(run_id)
        assert byte_pts[0].sum == expected_output.raw_size_bytes

    asyncio.run(_run())


def test_capture_handler_emits_error_telemetry_no_bytes(migrated_url: str) -> None:
    """capture_handler must emit duration with outcome='error' and no bytes on failure."""

    async def _run() -> None:
        reader = InMemoryMetricReader()
        meter = MeterProvider(metric_readers=[reader]).get_meter("test")
        telemetry = CaptureTelemetry(meter=meter)
        async with _pool(migrated_url) as pool:
            sys_id, run_id = await _crashed_run(pool)
            job = await _enqueue_capture(pool, run_id)
            err = CategorizedError("no core", category=ErrorCategory.READINESS_FAILURE)
            retriever = _FakeRetriever(run_id, raises=err)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await vmcore_plane.capture_handler(
                        conn,
                        job,
                        resolver=provider_resolver(retriever=retriever),
                        telemetry=telemetry,
                    )
        dur_pts = _metric_points(reader, "kdive.vmcore.capture.duration")
        byte_pts = _metric_points(reader, "kdive.vmcore.capture.bytes")
        assert dur_pts, "duration not emitted on error"
        assert dur_pts[0].attributes["outcome"] == "error"
        assert not byte_pts, "bytes must not be emitted on error"

    asyncio.run(_run())


# --- registration --------------------------------------------------------------------------


def test_register_handlers_binds_capture_vmcore() -> None:
    registry = HandlerRegistry()
    vmcore_plane.register_handlers(
        registry, resolver=provider_resolver(retriever=_FakeRetriever("x"))
    )
    assert registry.get(JobKind.CAPTURE_VMCORE) is not None
