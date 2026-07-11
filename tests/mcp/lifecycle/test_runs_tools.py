"""runs.* tool tests — handlers called directly with an injected pool + ctx."""

from __future__ import annotations

import asyncio
import copy
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString, cast
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, JOBS, RESOURCES, RUNS, SYSTEMS
from kdive.domain.capacity.state import (
    AllocationState,
    InvestigationState,
    JobState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation, Investigation, Run, System
from kdive.domain.lifecycle.run_steps import BootOutcome
from kdive.domain.operations.jobs import Job, JobKind
from kdive.domain.pcie import PCIeClaim
from kdive.jobs.handlers.console import console_evidence
from kdive.jobs.handlers.runs import common as run_handler_common
from kdive.jobs.handlers.runs import registrar as runs_handlers
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._runtime_resolution import with_runtime_for_run_target_kind
from kdive.mcp.tools.lifecycle.runs import common as runs_common
from kdive.mcp.tools.lifecycle.runs.bind import RunBindRequest, bind_run
from kdive.mcp.tools.lifecycle.runs.cancel import cancel_run
from kdive.mcp.tools.lifecycle.runs.create import (
    RunCreateRequest,
    RunReuseRequirementInput,
    create_run,
)
from kdive.mcp.tools.lifecycle.runs.create import _created_response as _created_response
from kdive.mcp.tools.lifecycle.runs.steps import boot_run, install_run
from kdive.mcp.tools.lifecycle.runs.view import get_run as _get_run
from kdive.mcp.tools.lifecycle.vmcore import view as vmcore_view
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.artifacts.listing import CONSOLE_MANIFEST_MAX, ConsoleManifest
from kdive.services.runs import steps as run_steps
from kdive.services.runs.admission import RunCreateResult
from kdive.services.runs.steps import StepProgress, ready_boot_outcome, step_progress
from tests.db_waits import wait_until_any_backend_waiting
from tests.mcp.systems_support import provider_resolver

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_PROFILE: dict[str, Any] = {"schema_version": 1}


@pytest.fixture(autouse=True)
def _staged_warm_tree(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage a usable warm tree so the worker-local build tests pass ADR-0158 admission.

    These tests inject a recording/failing builder for a worker-local (warm-tree) run; the
    build now admits ``KDIVE_KERNEL_SRC`` before the builder runs, so an unset value would
    reject every build at admission and mask each test's intended builder path. A real
    absolute directory lets admission pass through to the injected builder.
    """
    monkeypatch.setenv("KDIVE_KERNEL_SRC", str(tmp_path_factory.mktemp("warm-tree")))


def _profile() -> dict[str, Any]:
    return copy.deepcopy(_PROFILE)


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


def _run_model(
    state: RunState,
    *,
    failure: ErrorCategory | None = None,
    expected_boot_failure: dict[str, str] | None = None,
    target_kind: ResourceKind = ResourceKind.LOCAL_LIBVIRT,
) -> Run:
    return Run(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        investigation_id=uuid4(),
        system_id=uuid4(),
        target_kind=target_kind,
        state=state,
        build_profile=_profile(),
        expected_boot_failure=expected_boot_failure,
        failure_category=failure,
    )


def _job_model(state: JobState = JobState.QUEUED) -> Job:
    return Job(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=JobKind.BUILD,
        payload={"run_id": str(uuid4())},
        state=state,
        max_attempts=3,
        authorizing={"principal": "user-1", "agent_session": "s", "project": "proj"},
        dedup_key="run-build",
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def get_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    run_id: str,
    *,
    include_console_artifacts: bool = False,
) -> Any:
    return await _get_run(
        pool,
        ctx,
        run_id,
        resolver=provider_resolver(),
        include_console_artifacts=include_console_artifacts,
    )


async def _seed_system(
    pool: AsyncConnectionPool,
    *,
    system_state: SystemState = SystemState.READY,
    alloc_state: AllocationState = AllocationState.ACTIVE,
    project: str = "proj",
    provisioning_profile: dict[str, Any] | None = None,
    requested_vcpus: int | None = None,
    requested_memory_gb: int | None = None,
    requested_disk_gb: int | None = None,
    pcie_claim: list[PCIeClaim] | None = None,
    lease_expiry: datetime | None = None,
) -> str:
    """Insert a Resource + Allocation + System directly and return the system id."""
    async with pool.connection() as conn:
        res = await RESOURCES.insert(
            conn,
            Resource(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                kind=ResourceKind.LOCAL_LIBVIRT,
                pool="local-libvirt",
                cost_class="local",
                status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            ),
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                resource_id=res.id,
                state=alloc_state,
                requested_vcpus=requested_vcpus,
                requested_memory_gb=requested_memory_gb,
                requested_disk_gb=requested_disk_gb,
                pcie_claim=pcie_claim or [],
                lease_expiry=lease_expiry,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                allocation_id=alloc.id,
                state=system_state,
                provisioning_profile=provisioning_profile
                if provisioning_profile is not None
                else _profile_dump(),
            ),
        )
    return str(system.id)


async def _seed_investigation(
    pool: AsyncConnectionPool,
    *,
    state: InvestigationState = InvestigationState.OPEN,
    project: str = "proj",
) -> str:
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                title="seeded",
                state=state,
            ),
        )
    return str(inv.id)


async def _seed_run(
    pool: AsyncConnectionPool,
    *,
    state: RunState,
    failure: ErrorCategory | None = None,
    build_profile: dict[str, Any] | None = None,
    project: str = "proj",
    provisioning_profile: dict[str, Any] | None = None,
    label: str | None = None,
) -> str:
    inv_id = await _seed_investigation(pool, project=project)
    sys_id = await _seed_system(pool, project=project, provisioning_profile=provisioning_profile)
    async with pool.connection() as conn:
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                investigation_id=UUID(inv_id),
                system_id=UUID(sys_id),
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=state,
                build_profile=_profile() if build_profile is None else build_profile,
                failure_category=failure,
                label=label,
            ),
        )
    return str(run.id)


async def _seed_unbound_run(
    pool: AsyncConnectionPool,
    *,
    state: RunState = RunState.SUCCEEDED,
    target_kind: ResourceKind = ResourceKind.LOCAL_LIBVIRT,
    project: str = "proj",
) -> str:
    """Insert an Investigation + an unbound Run (system_id IS NULL) and return the run id."""
    inv_id = await _seed_investigation(pool, project=project)
    async with pool.connection() as conn:
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                investigation_id=UUID(inv_id),
                system_id=None,
                target_kind=target_kind,
                state=state,
                build_profile=_profile(),
            ),
        )
    return str(run.id)


async def _bind(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    run_id: str,
    sys_id: str,
    *,
    reuse_requirement: RunReuseRequirementInput | None = None,
):
    return await bind_run(
        pool,
        ctx,
        RunBindRequest(run_id=run_id, system_id=sys_id, reuse_requirement=reuse_requirement),
    )


def test_bind_unbound_run_succeeds(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_unbound_run(pool, state=RunState.SUCCEEDED)
            sys_id = await _seed_system(pool)
            resp = await _bind(pool, _ctx(), run_id, sys_id)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT system_id FROM runs WHERE id = %s", (run_id,))
                row = await cur.fetchone()
        assert resp.status == "bound"
        assert resp.data["system_id"] == sys_id
        assert "runs.install" in resp.suggested_next_actions
        assert row is not None and str(row["system_id"]) == sys_id

    asyncio.run(_run())


def test_bind_kind_mismatch_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_unbound_run(pool, target_kind=ResourceKind.REMOTE_LIBVIRT)
            sys_id = await _seed_system(pool)  # local-libvirt
            resp = await _bind(pool, _ctx(), run_id, sys_id)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT system_id FROM runs WHERE id = %s", (run_id,))
                row = await cur.fetchone()
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "target_kind_mismatch"
        assert row is not None and row["system_id"] is None

    asyncio.run(_run())


def test_bind_already_bound_run_is_conflict(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            sys_id = await _seed_system(pool)
            resp = await _bind(pool, _ctx(), run_id, sys_id)
        assert resp.status == "error" and resp.error_category == "transport_conflict"
        assert resp.data["reason"] == "run_already_bound"

    asyncio.run(_run())


def test_bind_terminal_run_is_stale(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_unbound_run(pool, state=RunState.FAILED)
            sys_id = await _seed_system(pool)
            resp = await _bind(pool, _ctx(), run_id, sys_id)
        assert resp.status == "error" and resp.error_category == "stale_handle"

    asyncio.run(_run())


def test_bind_system_with_live_run_is_conflict(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _seed_system(pool)
            occupant_inv = await _seed_investigation(pool)
            async with pool.connection() as conn:
                await RUNS.insert(
                    conn,
                    Run(
                        id=uuid4(),
                        created_at=_DT,
                        updated_at=_DT,
                        principal="user-1",
                        project="proj",
                        investigation_id=UUID(occupant_inv),
                        system_id=UUID(sys_id),
                        target_kind=ResourceKind.LOCAL_LIBVIRT,
                        state=RunState.RUNNING,
                        build_profile=_profile(),
                    ),
                )
            run_id = await _seed_unbound_run(pool, state=RunState.SUCCEEDED)
            resp = await _bind(pool, _ctx(), run_id, sys_id)
        assert resp.status == "error" and resp.error_category == "transport_conflict"

    asyncio.run(_run())


def test_install_unbound_run_is_not_bound(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_unbound_run(pool, state=RunState.SUCCEEDED)
            resp = await install_run(
                pool, _ctx(), run_id, resolver=provider_resolver(profile_policy=_LOCAL_POLICY)
            )
            n_jobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "run_not_bound"
        assert "runs.bind" in resp.suggested_next_actions
        assert n_jobs == 0

    asyncio.run(_run())


def test_boot_unbound_run_is_not_bound(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_unbound_run(pool, state=RunState.SUCCEEDED)
            resp = await boot_run(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "run_not_bound"
        assert "runs.bind" in resp.suggested_next_actions

    asyncio.run(_run())


def test_cancel_unbound_run_succeeds(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_unbound_run(pool, state=RunState.RUNNING)
            resp = await cancel_run(pool, _ctx(), run_id)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
                row = await cur.fetchone()
        assert resp.status == "canceled"
        assert row is not None and row["state"] == "canceled"

    asyncio.run(_run())


def test_with_runtime_for_run_target_kind_resolves_unbound_run(migrated_url: str) -> None:
    """The runs.complete_build admission path resolves an unbound Run (ADR-0169).

    The wrapper uses the target-kind helper, not the bound-run helper, because a Run can be built
    before it is attached to a System. The runtime is selected from the Run's committed
    target_kind.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            unbound = await _seed_unbound_run(pool, state=RunState.CREATED)
            bound = await _seed_run(pool, state=RunState.CREATED)
            seen: list[str] = []

            async def _cb(rid: str) -> ToolResponse:
                seen.append(rid)
                return ToolResponse.success(rid, "ok")

            unbound_resp = await with_runtime_for_run_target_kind(
                pool,
                provider_resolver(),
                _ctx(),
                unbound,
                lambda _r: _cb(unbound),
                required_role=Role.OPERATOR,
            )
            bound_resp = await with_runtime_for_run_target_kind(
                pool,
                provider_resolver(),
                _ctx(),
                bound,
                lambda _r: _cb(bound),
                required_role=Role.OPERATOR,
            )
        assert unbound_resp.status == "ok"
        assert bound_resp.status == "ok"
        assert set(seen) == {unbound, bound}

    asyncio.run(_run())


def test_envelope_for_run_failed_uses_run_failure_category() -> None:
    resp = runs_common.envelope_for_run(
        _run_model(RunState.FAILED, failure=ErrorCategory.BUILD_FAILURE)
    )

    assert resp.status == "error"
    assert resp.error_category == "build_failure"
    assert resp.data["current_status"] == "failed"
    assert "investigation_id" in resp.data
    assert resp.data["build_source"] == "external"  # every run is the upload lane
    assert "build_source_provenance" not in resp.data


def test_envelope_for_run_failed_defaults_to_infrastructure_failure() -> None:
    resp = runs_common.envelope_for_run(_run_model(RunState.FAILED))

    assert resp.status == "error"
    assert resp.error_category == "infrastructure_failure"


def _failed_job(failure_context: dict[str, str]) -> Job:
    job = _job_model(JobState.FAILED)
    return job.model_copy(
        update={"error_category": ErrorCategory.BUILD_FAILURE, "failure_context": failure_context}
    )


def test_envelope_for_run_failed_surfaces_linked_job_reason() -> None:
    job = _failed_job(
        {"failure_message": "make: defconfig: No such target", "failure_detail_run_id": "abc"}
    )
    resp = runs_common.envelope_for_run(
        _run_model(RunState.FAILED, failure=ErrorCategory.BUILD_FAILURE),
        failing_job=job,
    )

    assert resp.status == "error"
    assert resp.error_category == "build_failure"
    assert resp.detail == "make: defconfig: No such target"
    assert resp.data["failing_job_id"] == str(job.id)
    assert resp.data["failure_detail_run_id"] == "abc"


def test_envelope_for_run_failed_links_job_even_without_message() -> None:
    job = _failed_job({})
    resp = runs_common.envelope_for_run(
        _run_model(RunState.FAILED, failure=ErrorCategory.BUILD_FAILURE),
        failing_job=job,
    )

    assert resp.detail is None
    assert resp.data["failing_job_id"] == str(job.id)


def test_envelope_for_run_failed_surfaces_build_log_ref() -> None:
    # A failed build whose job recorded the build-log artifact id surfaces it as refs["build-log"]
    # so an agent resolves the captured compiler output via artifacts.get (#770, ADR-0238).
    job = _failed_job({"failure_detail_build_log_artifact": "11111111-1111-1111-1111-111111111111"})
    resp = runs_common.envelope_for_run(
        _run_model(RunState.FAILED, failure=ErrorCategory.BUILD_FAILURE),
        failing_job=job,
    )

    assert resp.refs["build-log"] == "11111111-1111-1111-1111-111111111111"


def test_envelope_for_run_failed_without_build_log_has_no_ref() -> None:
    job = _failed_job({"failure_message": "make exited non-zero"})
    resp = runs_common.envelope_for_run(
        _run_model(RunState.FAILED, failure=ErrorCategory.BUILD_FAILURE),
        failing_job=job,
    )

    assert "build-log" not in resp.refs


def test_envelope_for_run_failed_no_link_derives_detail_from_category() -> None:
    # No linked job (e.g. a reconciler-driven failure on a torn-down System): the failed Run is
    # never a bare category — `detail` is derived from `failure_category` (#516).
    resp = runs_common.envelope_for_run(
        _run_model(RunState.FAILED, failure=ErrorCategory.LEASE_EXPIRED)
    )

    assert resp.detail == runs_common.no_job_failure_detail(ErrorCategory.LEASE_EXPIRED)
    assert resp.detail
    assert "failing_job_id" not in resp.data


def test_envelope_for_run_failed_no_link_unmapped_category_has_generic_detail() -> None:
    # Any diagnostic category without a specific reason still gets a non-empty fallback so a
    # failed Run is never category-only (#516).
    resp = runs_common.envelope_for_run(
        _run_model(RunState.FAILED, failure=ErrorCategory.BUILD_FAILURE)
    )

    assert resp.detail
    assert "failing_job_id" not in resp.data


def test_envelope_for_run_failed_no_link_no_leak_category_stays_suppressed() -> None:
    # A no-leak category with no job must still surface only the seam constant — the derived
    # detail must not bypass the no-leak seam (ADR-0123, #516).
    resp = runs_common.envelope_for_run(
        _run_model(RunState.FAILED, failure=ErrorCategory.NOT_FOUND)
    )

    assert resp.detail == "not found"
    assert "failing_job_id" not in resp.data


def test_envelope_for_run_failed_suppresses_detail_for_no_leak_categories() -> None:
    # A linked job reason must never leak past the no-leak seam (ADR-0123): a not_found
    # failure surfaces the seam constant, not the job message, and surfaces NO job-derived
    # data (no failing_job_id, no failure_detail_* keys).
    job = _failed_job(
        {
            "failure_message": "secret-host-name leaked here",
            "failure_detail_host": "secret-host",
        }
    )
    resp = runs_common.envelope_for_run(
        _run_model(RunState.FAILED, failure=ErrorCategory.NOT_FOUND),
        failing_job=job,
    )

    assert resp.error_category == "not_found"
    assert resp.detail == "not found"
    assert "failing_job_id" not in resp.data
    assert "failure_detail_host" not in resp.data


def test_envelope_for_run_expected_boot_failure_detail_is_structured() -> None:
    expected = {
        "pattern": "__d_lookup|Oops",
        "kind": "console_crash",
        "description": "known crash",
    }
    resp = runs_common.envelope_for_run(
        _run_model(RunState.CREATED, expected_boot_failure=expected),
        required_cmdline="panic_on_oops=1",
    )

    assert resp.status == "created"
    assert resp.suggested_next_actions == ["runs.get", "runs.complete_build"]
    assert resp.data["required_cmdline"] == "panic_on_oops=1"
    assert resp.data["expected_boot_failure"] == "console_crash"
    assert resp.data["expected_boot_failure_detail"] == expected
    assert "expected_boot_failure_matched_line" not in resp.data


def test_envelope_for_run_surfaces_expected_boot_failure_matched_line() -> None:
    # The actual console line that matched, alongside the configured detail (#840, ADR-0260).
    expected = {"pattern": "__d_lookup", "kind": "console_crash"}
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED, expected_boot_failure=expected),
        step_progress=StepProgress(
            install="succeeded",
            boot="succeeded",
            boot_outcome="expected_crash_observed",
            matched_line="RIP: 0010:__d_lookup+0x1a/0x120",
        ),
    )

    assert resp.data["expected_boot_failure_detail"] == expected
    assert resp.data["expected_boot_failure_matched_line"] == "RIP: 0010:__d_lookup+0x1a/0x120"


def test_envelope_for_run_matched_line_absent_when_progress_has_none() -> None:
    expected = {"pattern": "__d_lookup", "kind": "console_crash"}
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED, expected_boot_failure=expected),
        step_progress=StepProgress(
            install="succeeded", boot="succeeded", boot_outcome="ready", matched_line=None
        ),
    )

    assert "expected_boot_failure_matched_line" not in resp.data


def test_envelope_for_run_surfaces_installed_cmdline() -> None:
    # runs.get read-back of the applied install cmdline (ADR-0299): confirms the live variant.
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED),
        step_progress=StepProgress(
            install="succeeded",
            boot="succeeded",
            boot_outcome="ready",
            installed_cmdline="dhash_entries=1",
        ),
    )

    assert resp.data["installed_cmdline"] == "dhash_entries=1"


def test_envelope_for_run_installed_cmdline_null_before_install() -> None:
    # A built-but-not-installed Run reports installed_cmdline null (nothing applied yet).
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED),
        step_progress=StepProgress(install="pending", boot="pending", boot_outcome=None),
    )

    assert resp.data["installed_cmdline"] is None


def test_envelope_for_run_omits_installed_cmdline_without_progress() -> None:
    # A created/running Run has no step progress, so the key is omitted (not a null claim).
    resp = runs_common.envelope_for_run(_run_model(RunState.RUNNING))
    assert "installed_cmdline" not in resp.data


def test_envelope_for_run_surfaces_installed_crashkernel() -> None:
    # runs.get read-back of the applied kdump reservation (ADR-0300): confirms the live value.
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED),
        step_progress=StepProgress(
            install="succeeded",
            boot="succeeded",
            boot_outcome="ready",
            installed_crashkernel="512M",
        ),
    )

    assert resp.data["installed_crashkernel"] == "512M"


def test_envelope_for_run_installed_crashkernel_null_when_default() -> None:
    # A Run on the default reservation reports installed_crashkernel null (256M default in force).
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED),
        step_progress=StepProgress(install="succeeded", boot="pending", boot_outcome=None),
    )

    assert resp.data["installed_crashkernel"] is None


def test_envelope_for_run_omits_installed_crashkernel_without_progress() -> None:
    # A created/running Run has no step progress, so the key is omitted (not a null claim).
    resp = runs_common.envelope_for_run(_run_model(RunState.RUNNING))
    assert "installed_crashkernel" not in resp.data


_CONSOLE_ACCESS_EXPECTED = {
    "ref": "console",
    "search": "artifacts.find",
    "full_text": "artifacts.get",
}


def test_envelope_for_run_surfaces_console_access_hint() -> None:
    # When refs.console is present, name the two VIEWER-accessible read paths for the
    # redacted console artifact so an agent learns both from the envelope (#864, ADR-0262).
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED),
        step_progress=StepProgress(
            install="succeeded",
            boot="succeeded",
            boot_outcome="ready",
            console_evidence_artifact_id="console-artifact-1",
        ),
    )

    assert resp.refs["console"] == "console-artifact-1"
    console_access = resp.data["console_access"]
    assert isinstance(console_access, dict)
    assert console_access == _CONSOLE_ACCESS_EXPECTED
    # fetch_raw cannot serve the console artifact and is contributor-gated; never named here.
    assert "artifacts.fetch_raw" not in console_access.values()


def _manifest_entry(name: str) -> dict[str, str]:
    return {"artifact_id": str(uuid4()), "object_key": name, "created_at": "2026-01-01T00:00:00+00"}


def test_envelope_for_run_renders_console_manifest() -> None:
    # ADR-0279: runs.get carries data.console_artifacts (the Run-scoped manifest); a non-truncated
    # manifest omits the total/truncated disclosure keys.
    entries = [_manifest_entry("console-part-0-000001"), _manifest_entry("console-part-0-000000")]
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED),
        console_manifest=ConsoleManifest(entries=entries, total=2),
    )

    assert resp.data["console_artifacts"] == entries
    assert "console_artifacts_total" not in resp.data
    assert "console_artifacts_truncated" not in resp.data


def test_envelope_for_run_console_manifest_truncation_disclosed() -> None:
    entries = [_manifest_entry(f"console-part-0-{i:06d}") for i in range(CONSOLE_MANIFEST_MAX)]
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED),
        console_manifest=ConsoleManifest(entries=entries, total=CONSOLE_MANIFEST_MAX + 5),
    )

    listed = resp.data["console_artifacts"]
    assert isinstance(listed, list)
    assert len(listed) == CONSOLE_MANIFEST_MAX
    assert resp.data["console_artifacts_total"] == CONSOLE_MANIFEST_MAX + 5
    assert resp.data["console_artifacts_truncated"] is True


def test_envelope_for_run_omits_empty_console_manifest() -> None:
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED),
        console_manifest=ConsoleManifest(entries=[], total=0),
    )
    assert "console_artifacts" not in resp.data
    # And None (the failed/no-query path) likewise omits it.
    assert (
        "console_artifacts" not in runs_common.envelope_for_run(_run_model(RunState.RUNNING)).data
    )


async def _seed_run_console_artifact(pool: AsyncConnectionPool, run_id: str, name: str) -> None:
    """Insert a redacted, Run-correlated console artifact owned by the Run's System."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT system_id FROM runs WHERE id = %s", (run_id,))
        row = await cur.fetchone()
        assert row is not None
        sys_id = row[0]
        await conn.execute(
            "INSERT INTO artifacts (created_at, updated_at, owner_kind, owner_id, object_key, "
            "etag, sensitivity, retention_class, run_id) "
            "VALUES (%s, %s, 'systems', %s, %s, 'e', 'redacted', 'console', %s)",
            (_DT, _DT, sys_id, f"local/systems/{sys_id}/{name}", run_id),
        )


def test_get_run_omits_console_manifest_by_default(migrated_url: str) -> None:
    # #1067 (ADR-0324): runs.get is a per-token status read; the Run-scoped console manifest is
    # opt-in. Default reads must not inline data.console_artifacts even when the Run has console.
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _seed_run_console_artifact(pool, run_id, f"console-{run_id}")
            return await get_run(pool, _ctx(), run_id)

    resp = asyncio.run(_run())
    assert "console_artifacts" not in resp.data
    assert "console_artifacts_total" not in resp.data
    assert "console_artifacts_truncated" not in resp.data


def test_get_run_includes_console_manifest_when_opted_in(migrated_url: str) -> None:
    # include_console_artifacts=True restores the ADR-0279 inlined manifest verbatim.
    async def _run() -> tuple[ToolResponse, str]:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _seed_run_console_artifact(pool, run_id, f"console-{run_id}")
            return await get_run(pool, _ctx(), run_id, include_console_artifacts=True), run_id

    resp, run_id = asyncio.run(_run())
    listed = cast(list[dict[str, str]], resp.data["console_artifacts"])
    assert len(listed) == 1
    assert listed[0]["object_key"].endswith(f"console-{run_id}")
    assert set(listed[0]) == {"artifact_id", "object_key", "created_at"}


def test_envelope_for_run_console_access_hint_for_expected_crash() -> None:
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED),
        step_progress=StepProgress(
            install="succeeded",
            boot="succeeded",
            boot_outcome="expected_crash_observed",
            console_evidence_artifact_id="console-artifact-2",
        ),
    )

    assert resp.refs["console"] == "console-artifact-2"
    assert resp.data["console_access"] == _CONSOLE_ACCESS_EXPECTED


def test_envelope_for_run_console_access_hint_absent_without_console_ref() -> None:
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED),
        step_progress=StepProgress(
            install="succeeded",
            boot="succeeded",
            boot_outcome="ready",
            console_evidence_artifact_id=None,
        ),
    )

    assert "console" not in resp.refs
    assert "console_access" not in resp.data


def test_envelope_for_run_console_access_hint_absent_without_step_progress() -> None:
    resp = runs_common.envelope_for_run(_run_model(RunState.SUCCEEDED))

    assert "console" not in resp.refs
    assert "console_access" not in resp.data


def _ready_progress(boot_outcome: BootOutcome | None) -> StepProgress:
    return StepProgress(install="succeeded", boot="succeeded", boot_outcome=boot_outcome)


def test_envelope_for_run_surfaces_ready_boot_outcome() -> None:
    # Success-path symmetry to the failure side: the structured "ready" descriptor (#837).
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED), step_progress=_ready_progress("ready")
    )

    assert resp.data["boot_outcome"] == ready_boot_outcome()


def test_envelope_for_run_ready_boot_outcome_absent_for_expected_crash() -> None:
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED),
        step_progress=_ready_progress("expected_crash_observed"),
    )

    assert "boot_outcome" not in resp.data


def test_envelope_for_run_ready_boot_outcome_absent_without_outcome() -> None:
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED), step_progress=_ready_progress(None)
    )

    assert "boot_outcome" not in resp.data


def test_envelope_for_run_ready_boot_outcome_omitted_for_remote_libvirt() -> None:
    # Remote-libvirt confirms readiness by a boot-id change (ADR-0082), not the console marker, so
    # the console-marker descriptor would misreport a remote boot — it is omitted (ADR-0254, #837).
    resp = runs_common.envelope_for_run(
        _run_model(RunState.SUCCEEDED, target_kind=ResourceKind.REMOTE_LIBVIRT),
        step_progress=_ready_progress("ready"),
    )

    assert "boot_outcome" not in resp.data


@pytest.mark.parametrize(
    ("state", "actions"),
    [
        (RunState.CREATED, ["runs.get", "runs.complete_build"]),
        (RunState.RUNNING, ["runs.get", "runs.complete_build"]),
        (RunState.SUCCEEDED, ["runs.get", "runs.install"]),
        (RunState.CANCELED, ["runs.get"]),
    ],
)
def test_envelope_for_run_suggests_next_action_per_state(
    state: RunState, actions: list[str]
) -> None:
    # `_run_model` is a bound Run, so SUCCEEDED advances to install (unbound would be bind).
    resp = runs_common.envelope_for_run(_run_model(state))

    assert resp.status == state.value
    assert resp.suggested_next_actions == actions
    assert resp.data["project"] == "proj"
    assert resp.data["target_kind"] == "local-libvirt"
    assert "system_id" in resp.data


def test_run_job_envelope_adds_run_id_to_standard_job_envelope() -> None:
    run_id = uuid4()
    job = _job_model()

    resp = runs_common.run_job_envelope(job, run_id)

    assert resp.object_id == str(job.id)
    assert resp.status == "queued"
    assert resp.suggested_next_actions == ["jobs.wait", "jobs.cancel"]
    assert resp.data == {"kind": "build", "run_id": str(run_id)}


def test_get_unbound_succeeded_run_points_to_bind(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_unbound_run(pool, state=RunState.SUCCEEDED)
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.status == "succeeded"
        assert resp.suggested_next_actions == ["runs.get", "runs.bind"]
        assert resp.data["system_id"] is None
        assert resp.data["target_kind"] == "local-libvirt"

    asyncio.run(_run())


def test_get_bound_succeeded_run_points_to_install(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.status == "succeeded"
        assert resp.suggested_next_actions == ["runs.get", "runs.install"]
        assert resp.data["target_kind"] == "local-libvirt"

    asyncio.run(_run())


def test_get_created_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.status == "created"
        assert resp.suggested_next_actions == ["runs.get", "runs.complete_build"]

    asyncio.run(_run())


def test_get_run_echoes_label(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            labeled = await _seed_run(pool, state=RunState.CREATED, label="repro-A")
            unlabeled = await _seed_run(pool, state=RunState.CREATED)
            labeled_resp = await get_run(pool, _ctx(), labeled)
            unlabeled_resp = await get_run(pool, _ctx(), unlabeled)
        assert labeled_resp.data["label"] == "repro-A"
        assert unlabeled_resp.data["label"] is None

    asyncio.run(_run())


def test_get_failed_run_carries_label(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool,
                state=RunState.FAILED,
                failure=ErrorCategory.BUILD_FAILURE,
                label="repro-fail",
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.error_category == ErrorCategory.BUILD_FAILURE
        assert resp.data["label"] == "repro-fail"

    asyncio.run(_run())


async def _insert_step(
    pool: AsyncConnectionPool, run_id: str, step: str, state: str, result: dict[str, Any]
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) VALUES (%s, %s, %s, %s)",
            (UUID(run_id), step, state, Jsonb(result)),
        )


def test_step_progress_reads_install_boot_and_outcome(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO run_steps (run_id, step, state, result) "
                    "VALUES (%s, 'install', 'succeeded', %s)",
                    (UUID(run_id), Jsonb({})),
                )
                await conn.execute(
                    "INSERT INTO run_steps (run_id, step, state, result) "
                    "VALUES (%s, 'boot', 'succeeded', %s)",
                    (UUID(run_id), Jsonb({"boot_outcome": "expected_crash_observed"})),
                )
                progress = await step_progress(conn, UUID(run_id))
        assert progress == StepProgress(
            install="succeeded",
            boot="succeeded",
            boot_outcome="expected_crash_observed",
            console_evidence_artifact_id=None,
        )
        assert progress.steps_map() == {
            "build": "succeeded",
            "install": "succeeded",
            "boot": "succeeded",
        }

    asyncio.run(_run())


def test_step_progress_missing_rows_are_pending(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            async with pool.connection() as conn:
                progress = await step_progress(conn, UUID(run_id))
        assert progress == StepProgress(
            install="pending", boot="pending", boot_outcome=None, console_evidence_artifact_id=None
        )

    asyncio.run(_run())


def test_step_progress_reads_console_evidence_artifact_id(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            evidence_id = str(uuid4())
            await _insert_step(
                pool,
                run_id,
                "boot",
                "succeeded",
                {"boot_outcome": "ready", "evidence_artifact_id": evidence_id},
            )
            async with pool.connection() as conn:
                progress = await step_progress(conn, UUID(run_id))
        assert progress.console_evidence_artifact_id == evidence_id

    asyncio.run(_run())


def test_step_progress_non_string_evidence_id_is_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(
                pool,
                run_id,
                "boot",
                "succeeded",
                {"boot_outcome": "ready", "evidence_artifact_id": 12345},
            )
            async with pool.connection() as conn:
                progress = await step_progress(conn, UUID(run_id))
        assert progress.console_evidence_artifact_id is None

    asyncio.run(_run())


def test_step_progress_surfaces_capture_disclosure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(
                pool,
                run_id,
                "boot",
                "succeeded",
                {
                    "boot_outcome": "expected_crash_observed",
                    "available_capture": ["console"],
                    "inert_capture": ["gdbstub", "host_dump"],
                },
            )
            async with pool.connection() as conn:
                progress = await step_progress(conn, UUID(run_id))
        assert progress.available_capture == ["console"]
        assert progress.inert_capture == ["gdbstub", "host_dump"]

    asyncio.run(_run())


def test_step_progress_capture_disclosure_absent_is_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "boot", "succeeded", {"boot_outcome": "ready"})
            async with pool.connection() as conn:
                progress = await step_progress(conn, UUID(run_id))
        assert progress.available_capture is None
        assert progress.inert_capture is None
        assert progress.matched_line is None


def test_step_progress_reads_matched_line(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(
                pool,
                run_id,
                "boot",
                "succeeded",
                {
                    "boot_outcome": "expected_crash_observed",
                    "matched_line": "RIP: 0010:__d_lookup+0x1a/0x120",
                },
            )
            async with pool.connection() as conn:
                progress = await step_progress(conn, UUID(run_id))
        assert progress.matched_line == "RIP: 0010:__d_lookup+0x1a/0x120"

    asyncio.run(_run())

    asyncio.run(_run())


def test_get_built_only_run_steps_and_install_action(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["steps"] == {
            "build": "succeeded",
            "install": "pending",
            "boot": "pending",
        }
        assert resp.suggested_next_actions == ["runs.get", "runs.install"]

    asyncio.run(_run())


def test_get_install_running_run_recommends_install(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "running", {})
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["steps"]["install"] == "running"
        assert resp.suggested_next_actions == ["runs.get", "runs.install"]

    asyncio.run(_run())


def test_get_installed_run_recommends_boot(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["steps"] == {
            "build": "succeeded",
            "install": "succeeded",
            "boot": "pending",
        }
        assert resp.suggested_next_actions == ["runs.get", "runs.boot"]

    asyncio.run(_run())


def test_get_booted_run_recommends_debug_start_session(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(pool, run_id, "boot", "succeeded", {"boot_outcome": "ready"})
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["steps"]["boot"] == "succeeded"
        assert resp.suggested_next_actions == ["runs.get", "debug.start_session"]

    asyncio.run(_run())


def test_get_expected_crash_boot_recommends_triage(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(
                pool, run_id, "boot", "succeeded", {"boot_outcome": "expected_crash_observed"}
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.suggested_next_actions == ["runs.get", "postmortem.triage", "vmcore.fetch"]

    asyncio.run(_run())


def test_get_expected_crash_surfaces_capture_disclosure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(
                pool,
                run_id,
                "boot",
                "succeeded",
                {
                    "boot_outcome": "expected_crash_observed",
                    "available_capture": ["console"],
                    "inert_capture": ["gdbstub", "host_dump"],
                },
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["available_capture"] == ["console"]
        assert resp.data["inert_capture"] == ["gdbstub", "host_dump"]
        assert resp.data["inert_capture_reason"] == vmcore_view.CONSOLE_CRASH_GUIDANCE

    asyncio.run(_run())


def test_get_inert_capture_reason_only_for_expected_crash(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(
                pool,
                run_id,
                "boot",
                "succeeded",
                {"boot_outcome": "ready", "inert_capture": ["gdbstub"]},
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["inert_capture"] == ["gdbstub"]
        assert "inert_capture_reason" not in resp.data

    asyncio.run(_run())


def test_get_boot_without_disclosure_omits_capture_keys(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(
                pool, run_id, "boot", "succeeded", {"boot_outcome": "expected_crash_observed"}
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert "available_capture" not in resp.data
        assert "inert_capture" not in resp.data

    asyncio.run(_run())


def test_get_ready_boot_surfaces_boot_outcome(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(pool, run_id, "boot", "succeeded", {"boot_outcome": "ready"})
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["boot_outcome"] == ready_boot_outcome()

    asyncio.run(_run())


async def _seed_boot_job(
    pool: AsyncConnectionPool,
    run_id: str,
    *,
    state: JobState,
    error_category: ErrorCategory | None = None,
) -> str:
    """Insert a boot job for ``run_id`` under its deterministic ``dedup_key`` (#750).

    Seeds the terminal state directly (no real worker) so the read-side helper is tested in
    isolation. The ``dedup_key`` mirrors ``_enqueue_step``'s ``f"{run.id}:boot"``.
    """
    job_id = uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO jobs (id, kind, payload, state, max_attempts, authorizing, dedup_key, "
            "    error_category) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                job_id,
                JobKind.BOOT.value,
                Jsonb({"run_id": run_id}),
                state.value,
                3,
                Jsonb({"principal": "user-1", "agent_session": "s", "project": "proj"}),
                f"{run_id}:boot",
                error_category.value if error_category is not None else None,
            ),
        )
    return str(job_id)


def test_failed_boot_attempt_none_when_no_boot_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            async with pool.connection() as conn:
                attempt = await run_steps.failed_boot_attempt(conn, UUID(run_id))
        assert attempt is None

    asyncio.run(_run())


def test_failed_boot_attempt_none_for_queued_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _seed_boot_job(pool, run_id, state=JobState.QUEUED)
            async with pool.connection() as conn:
                attempt = await run_steps.failed_boot_attempt(conn, UUID(run_id))
        assert attempt is None

    asyncio.run(_run())


def test_failed_boot_attempt_none_for_running_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _seed_boot_job(pool, run_id, state=JobState.RUNNING)
            async with pool.connection() as conn:
                attempt = await run_steps.failed_boot_attempt(conn, UUID(run_id))
        assert attempt is None

    asyncio.run(_run())


def test_failed_boot_attempt_surfaces_failed_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            job_id = await _seed_boot_job(
                pool,
                run_id,
                state=JobState.FAILED,
                error_category=ErrorCategory.READINESS_FAILURE,
            )
            async with pool.connection() as conn:
                attempt = await run_steps.failed_boot_attempt(conn, UUID(run_id))
        assert attempt is not None
        assert attempt.job_id == UUID(job_id)
        assert attempt.error_category is ErrorCategory.READINESS_FAILURE
        assert attempt.as_data() == {
            "job_id": job_id,
            "status": "failed",
            "error_category": "readiness_failure",
        }

    asyncio.run(_run())


def test_failed_boot_attempt_null_category(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _seed_boot_job(pool, run_id, state=JobState.FAILED, error_category=None)
            async with pool.connection() as conn:
                attempt = await run_steps.failed_boot_attempt(conn, UUID(run_id))
        assert attempt is not None
        assert attempt.error_category is None
        assert attempt.as_data() == {
            "job_id": str(attempt.job_id),
            "status": "failed",
            "error_category": None,
        }

    asyncio.run(_run())


def test_get_run_surfaces_failed_boot_attempt(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            # Boot terminally failed: the boot run_steps row was deleted (ADR-0185), and the
            # boot job survives as `failed` with its category (#750).
            job_id = await _seed_boot_job(
                pool,
                run_id,
                state=JobState.FAILED,
                error_category=ErrorCategory.READINESS_FAILURE,
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["steps"]["boot"] == "pending"
        assert resp.data["boot_readiness"] == {
            "job_id": job_id,
            "status": "failed",
            "error_category": "readiness_failure",
        }

    asyncio.run(_run())


def test_get_run_no_boot_readiness_when_never_attempted(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["steps"]["boot"] == "pending"
        assert "boot_readiness" not in resp.data

    asyncio.run(_run())


def test_get_run_no_boot_readiness_for_inflight_boot(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _seed_boot_job(pool, run_id, state=JobState.QUEUED)
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["steps"]["boot"] == "pending"
        assert "boot_readiness" not in resp.data

    asyncio.run(_run())


def test_get_run_no_boot_readiness_when_boot_succeeded(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(pool, run_id, "boot", "succeeded", {"boot_outcome": "ready"})
            # A stale failed boot job must not surface once the boot step has succeeded.
            await _seed_boot_job(
                pool,
                run_id,
                state=JobState.FAILED,
                error_category=ErrorCategory.READINESS_FAILURE,
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["steps"]["boot"] == "succeeded"
        assert "boot_readiness" not in resp.data

    asyncio.run(_run())


def test_get_booted_run_surfaces_console_ref(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            evidence_id = str(uuid4())
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(
                pool,
                run_id,
                "boot",
                "succeeded",
                {"boot_outcome": "ready", "evidence_artifact_id": evidence_id},
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.refs["console"] == evidence_id

    asyncio.run(_run())


def test_get_expected_crash_run_surfaces_console_ref(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            evidence_id = str(uuid4())
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(
                pool,
                run_id,
                "boot",
                "succeeded",
                {"boot_outcome": "expected_crash_observed", "evidence_artifact_id": evidence_id},
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.refs["console"] == evidence_id

    asyncio.run(_run())


def test_get_booted_run_without_evidence_has_no_console_ref(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(pool, run_id, "boot", "succeeded", {"boot_outcome": "ready"})
            resp = await get_run(pool, _ctx(), run_id)
        assert "console" not in resp.refs

    asyncio.run(_run())


def test_get_unbooted_run_has_no_console_ref(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            resp = await get_run(pool, _ctx(), run_id)
        assert "console" not in resp.refs

    asyncio.run(_run())


def test_get_non_succeeded_run_has_no_steps(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            resp = await get_run(pool, _ctx(), run_id)
        assert "steps" not in resp.data

    asyncio.run(_run())


def test_get_run_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            with pytest.raises(AuthorizationError):
                await get_run(pool, _ctx(role=None), run_id)

    asyncio.run(_run())


def test_get_failed_run_renders_failure_category(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.FAILED, failure=ErrorCategory.BUILD_FAILURE
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "build_failure"
        assert resp.data["current_status"] == "failed"

    asyncio.run(_run())


async def _seed_failed_build_job(
    pool: AsyncConnectionPool, run_id: str, failure_context: dict[str, str]
) -> str:
    """Insert a dead-lettered BUILD job and link it from the Run via failing_job_id."""
    async with pool.connection() as conn:
        job = await JOBS.insert(
            conn,
            Job(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                kind=JobKind.BUILD,
                payload={"run_id": run_id},
                state=JobState.FAILED,
                max_attempts=3,
                error_category=ErrorCategory.BUILD_FAILURE,
                failure_context=failure_context,
                authorizing={"principal": "user-1", "agent_session": "s", "project": "proj"},
                dedup_key=f"{run_id}:build",
            ),
        )
        await conn.execute("UPDATE runs SET failing_job_id = %s WHERE id = %s", (job.id, run_id))
    return str(job.id)


def test_get_failed_run_surfaces_linked_job_reason(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.FAILED, failure=ErrorCategory.BUILD_FAILURE
            )
            job_id = await _seed_failed_build_job(
                pool, run_id, {"failure_message": "make: defconfig: No such target"}
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "build_failure"
        assert resp.detail == "make: defconfig: No such target"
        assert resp.data["failing_job_id"] == job_id

    asyncio.run(_run())


def test_get_failed_run_links_job_without_message(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.FAILED, failure=ErrorCategory.BUILD_FAILURE
            )
            job_id = await _seed_failed_build_job(pool, run_id, {})
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.detail is None
        assert resp.data["failing_job_id"] == job_id

    asyncio.run(_run())


def test_get_failed_run_null_category_defaults_infra(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.FAILED, failure=None)
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "infrastructure_failure"
        # A no-job failure (here a NULL category defaulting to infra) is never bare (#516).
        assert resp.detail

    asyncio.run(_run())


def test_get_failed_run_no_job_reconciler_failure_has_detail(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.FAILED, failure=ErrorCategory.LEASE_EXPIRED
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "lease_expired"
        assert resp.detail == runs_common.no_job_failure_detail(ErrorCategory.LEASE_EXPIRED)
        assert "failing_job_id" not in resp.data

    asyncio.run(_run())


def test_get_canceled_run_is_success(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CANCELED)
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.status == "canceled"

    asyncio.run(_run())


def test_get_cross_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            resp = await get_run(pool, _ctx(projects=("other",)), run_id)
        assert resp.status == "error" and resp.error_category == "not_found"
        # ADR-0174 / AC#5: a valid id outside the caller's visibility stays a bare no-leak
        # not_found — no reason key, the suppressed-constant detail, identical to an absent id.
        assert "reason" not in resp.data
        assert resp.detail == "not found"

    asyncio.run(_run())


def test_get_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await get_run(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error" and resp.error_category == "configuration_error"
        # ADR-0174: actionable reason + non-null detail for the malformed-id parse failure.
        assert resp.data["reason"] == "invalid_uuid"
        assert resp.detail is not None and "not-a-uuid" in resp.detail

    asyncio.run(_run())


def test_get_run_exposes_expected_boot_failure(migrated_url: str) -> None:
    expected = {"kind": "console_crash", "pattern": "__d_lookup|Oops"}

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE runs SET expected_boot_failure = %s WHERE id = %s",
                    (Jsonb(expected), run_id),
                )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["expected_boot_failure"] == "console_crash"
        assert resp.data["expected_boot_failure_detail"] == expected

    asyncio.run(_run())


def test_get_run_surfaces_expected_boot_failure_matched_line(migrated_url: str) -> None:
    expected = {"kind": "console_crash", "pattern": "__d_lookup"}
    matched = "RIP: 0010:__d_lookup+0x1a/0x120"

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE runs SET expected_boot_failure = %s WHERE id = %s",
                    (Jsonb(expected), run_id),
                )
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(
                pool,
                run_id,
                "boot",
                "succeeded",
                {"boot_outcome": "expected_crash_observed", "matched_line": matched},
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["expected_boot_failure_detail"] == expected
        assert resp.data["expected_boot_failure_matched_line"] == matched

    asyncio.run(_run())


def _create_result() -> RunCreateResult:
    return RunCreateResult(
        run_id=uuid4(),
        project="proj",
        investigation_id=uuid4(),
        target_kind=ResourceKind.LOCAL_LIBVIRT,
        system_id=None,
    )


def test_created_response_chains_to_the_upload_loop() -> None:
    resp = _created_response(_create_result())
    assert resp.status == "created"
    assert resp.suggested_next_actions == [
        "runs.get",
        "artifacts.expected_uploads",
        "artifacts.feature_config_requirements",
        "artifacts.create_run_upload",
    ]


def test_create_external_run_chains_to_upload_loop(migrated_url: str) -> None:
    """The build_profile source flows to the response: external create points at the upload loop."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                profile={"schema_version": 1},
            )
        assert resp.status == "created"
        assert resp.suggested_next_actions == [
            "runs.get",
            "artifacts.expected_uploads",
            "artifacts.feature_config_requirements",
            "artifacts.create_run_upload",
        ]

    asyncio.run(_run())


async def _create(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    inv_id: str,
    sys_id: str,
    *,
    profile=None,
    reuse_requirement: RunReuseRequirementInput | None = None,
    idempotency_key: str | None = None,
    label: str | None = None,
):
    return await create_run(
        pool,
        ctx,
        RunCreateRequest(
            investigation_id=inv_id,
            system_id=sys_id,
            build_profile=profile or _profile(),
            reuse_requirement=reuse_requirement,
            label=label,
        ),
        resolver=provider_resolver(),
        idempotency_key=idempotency_key,
    )


def test_create_with_label_echoes_and_persists(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id, label="repro-A")
            assert resp.status == "created"
            assert resp.data["label"] == "repro-A"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT label FROM runs WHERE id = %s", (resp.object_id,))
                row = await cur.fetchone()
        assert row is not None and row["label"] == "repro-A"

    asyncio.run(_run())


def test_create_without_label_stores_null(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
            assert resp.data["label"] is None
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT label FROM runs WHERE id = %s", (resp.object_id,))
                row = await cur.fetchone()
        assert row is not None and row["label"] is None

    asyncio.run(_run())


def test_create_label_is_stored_stripped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id, label="  spaced run  ")
            assert resp.data["label"] == "spaced run"

    asyncio.run(_run())


def test_create_invalid_label_rejected_with_no_row_or_audit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id, label="bad\nlabel")
            assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR
            assert resp.data["reason"] == "invalid_label"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM runs WHERE investigation_id = %s", (inv_id,)
                )
                runs = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM audit_log WHERE tool = 'runs.create'")
                audits = await cur.fetchone()
        assert runs is not None and runs["n"] == 0
        assert audits is not None and audits["n"] == 0

    asyncio.run(_run())


def test_create_keyed_replay_keeps_first_label(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            first = await _create(pool, _ctx(), inv_id, sys_id, idempotency_key="k1", label="first")
            second = await _create(
                pool, _ctx(), inv_id, sys_id, idempotency_key="k1", label="second"
            )
        assert first.data["label"] == "first"
        assert second.data["label"] == "first"

    asyncio.run(_run())


def test_create_keyed_retry_replays_one_run(migrated_url: str) -> None:
    """Canonical #619 acceptance: a keyed retry returns the identical envelope, one Run row."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            first = await _create(pool, _ctx(), inv_id, sys_id, idempotency_key="k1")
            assert first.status == "created"
            # Simulate a transport drop: the first envelope never reached the client; it retries.
            second = await _create(pool, _ctx(), inv_id, sys_id, idempotency_key="k1")
            assert second.model_dump() == first.model_dump()
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM runs WHERE investigation_id = %s", (inv_id,)
                )
                runs = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM idempotency_keys WHERE kind = 'runs.create'"
                )
                keys = await cur.fetchone()
        assert runs is not None and runs["n"] == 1
        assert keys is not None and keys["n"] == 1

    asyncio.run(_run())


def test_create_unkeyed_calls_create_two_runs(migrated_url: str) -> None:
    """Without a key, two creates make two Runs (today's behavior, unchanged)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            first = await _create(pool, _ctx(), inv_id, sys_id)
            sys_id2 = await _seed_system(pool)
            second = await _create(pool, _ctx(), inv_id, sys_id2)
            assert first.object_id != second.object_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM runs WHERE investigation_id = %s", (inv_id,)
                )
                runs = await cur.fetchone()
        assert runs is not None and runs["n"] == 2

    asyncio.run(_run())


def test_create_first_run_flips_investigation_active(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
            assert resp.status == "created"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, build_profile FROM runs WHERE id = %s", (resp.object_id,)
                )
                run_row = await cur.fetchone()
                await cur.execute(
                    "SELECT state, last_run_at FROM investigations WHERE id = %s", (inv_id,)
                )
                inv_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'open->active' "
                    "AND object_id = %s",
                    (inv_id,),
                )
                flip = await cur.fetchone()
        assert run_row is not None and run_row["state"] == "created"
        assert run_row["build_profile"] == {"schema_version": 1}
        assert inv_row is not None and inv_row["state"] == "active"
        assert inv_row["last_run_at"] is not None
        assert flip is not None and flip["n"] == 1

    asyncio.run(_run())


def test_create_unbound_run_succeeds(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            resp = await create_run(
                pool,
                _ctx(),
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=None,
                    build_profile=_profile(),
                    target_kind="local-libvirt",
                ),
                resolver=provider_resolver(),
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT system_id, target_kind, state FROM runs WHERE id = %s",
                    (resp.object_id,),
                )
                row = await cur.fetchone()
                await cur.execute("SELECT state FROM investigations WHERE id = %s", (inv_id,))
                inv = await cur.fetchone()
        assert resp.status == "created"
        assert row is not None and row["system_id"] is None
        assert row["target_kind"] == "local-libvirt"
        assert resp.data["system_id"] is None
        assert resp.data["target_kind"] == "local-libvirt"
        assert "artifacts.expected_uploads" in resp.suggested_next_actions
        assert inv is not None and inv["state"] == "active"

    asyncio.run(_run())


def test_create_unbound_missing_target_kind_lists_available(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            resp = await create_run(
                pool,
                _ctx(),
                RunCreateRequest(investigation_id=inv_id, system_id=None, build_profile=_profile()),
                resolver=provider_resolver(),
            )
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM runs")
                count_row = await cur.fetchone()
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "target_kind_required"
        available = resp.data["available_target_kinds"]
        assert isinstance(available, list) and "local-libvirt" in available
        assert count_row is not None and count_row[0] == 0

    asyncio.run(_run())


def test_create_unbound_unknown_target_kind_lists_available(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            resp = await create_run(
                pool,
                _ctx(),
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=None,
                    build_profile=_profile(),
                    target_kind="remote-libvirt",
                ),
                resolver=provider_resolver(),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "unknown_target_kind"
        available = resp.data["available_target_kinds"]
        assert isinstance(available, list) and "local-libvirt" in available

    asyncio.run(_run())


def test_create_unbound_with_reuse_requirement_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            resp = await create_run(
                pool,
                _ctx(),
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=None,
                    build_profile=_profile(),
                    target_kind="local-libvirt",
                    reuse_requirement=RunReuseRequirementInput(vcpus=2),
                ),
                resolver=provider_resolver(),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "reuse_requires_system"

    asyncio.run(_run())


def test_create_bound_explicit_target_kind_mismatch(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await create_run(
                pool,
                _ctx(),
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=sys_id,
                    build_profile=_profile(),
                    target_kind="remote-libvirt",
                ),
                resolver=provider_resolver(),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "target_kind_mismatch"

    asyncio.run(_run())


def test_create_bound_stores_derived_target_kind(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT target_kind FROM runs WHERE id = %s", (resp.object_id,))
                row = await cur.fetchone()
        assert resp.status == "created"
        assert row is not None and row["target_kind"] == "local-libvirt"
        assert resp.data["target_kind"] == "local-libvirt"

    asyncio.run(_run())


def test_create_rejects_empty_build_profile(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            # Call create_run directly: the _create helper's `profile or _profile()` would
            # coalesce a falsy {} away, so it cannot exercise the empty-profile path.
            resp = await create_run(
                pool,
                _ctx(),
                RunCreateRequest(investigation_id=inv_id, system_id=sys_id, build_profile={}),
                resolver=provider_resolver(),
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM runs")
                row = await cur.fetchone()
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert row is not None and row["n"] == 0

    asyncio.run(_run())


def test_create_run_persists_expected_boot_failure(migrated_url: str) -> None:
    expected = {
        "kind": "console_crash",
        "pattern": "__d_lookup|Oops",
        "description": "dcache crash",
    }

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await create_run(
                pool,
                _ctx(),
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=sys_id,
                    build_profile=_profile(),
                    expected_boot_failure=expected,
                ),
                resolver=provider_resolver(),
            )
            assert resp.status == "created"
            assert resp.data["expected_boot_failure"] == "console_crash"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT expected_boot_failure FROM runs WHERE id = %s",
                    (resp.object_id,),
                )
                row = await cur.fetchone()
        assert row is not None
        assert row["expected_boot_failure"] == expected

    asyncio.run(_run())


def test_create_run_rejects_bad_expected_boot_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await create_run(
                pool,
                _ctx(),
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=sys_id,
                    build_profile=_profile(),
                    expected_boot_failure={"kind": "console_crash", "pattern": ""},
                ),
                resolver=provider_resolver(),
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM runs")
                row = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert row is not None and row["n"] == 0

    asyncio.run(_run())


def test_create_second_run_no_second_flip(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_a = await _seed_system(pool)
            sys_b = await _seed_system(pool)
            await _create(pool, _ctx(), inv_id, sys_a)
            resp = await _create(pool, _ctx(), inv_id, sys_b)
            assert resp.status == "created"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'open->active' "
                    "AND object_id = %s",
                    (inv_id,),
                )
                flip = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM runs WHERE investigation_id = %s", (inv_id,)
                )
                runs = await cur.fetchone()
        assert flip is not None and flip["n"] == 1  # flipped exactly once
        assert runs is not None and runs["n"] == 2

    asyncio.run(_run())


@pytest.mark.parametrize("state", [SystemState.TORN_DOWN, SystemState.FAILED, SystemState.CRASHED])
def test_create_on_gone_system_is_stale_handle(migrated_url: str, state: SystemState) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool, system_state=state)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
        assert resp.status == "error" and resp.error_category == "stale_handle"
        assert resp.data["current_status"] == state.value

    asyncio.run(_run())


@pytest.mark.parametrize("state", [SystemState.DEFINED, SystemState.PROVISIONING])
def test_create_on_not_ready_system_is_config_error(migrated_url: str, state: SystemState) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool, system_state=state)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == state.value

    asyncio.run(_run())


def test_create_with_non_active_allocation_is_stale_handle(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            # System ready, but its Allocation is released (the orphaned-System window).
            sys_id = await _seed_system(pool, alloc_state=AllocationState.RELEASED)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
        assert resp.status == "error" and resp.error_category == "stale_handle"
        assert resp.data["current_status"] == "released"

    asyncio.run(_run())


@pytest.mark.parametrize("state", [InvestigationState.CLOSED, InvestigationState.ABANDONED])
def test_create_on_terminal_investigation_is_config_error(
    migrated_url: str, state: InvestigationState
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=state)
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == state.value

    asyncio.run(_run())


def test_create_cross_project_join_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _seed_system(pool, project="proj")
            other_inv = await _seed_investigation(pool, project="proj")
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE investigations SET project = 'p2' WHERE id = %s", (other_inv,)
                )
            ctx = RequestContext(
                principal="user-1",
                agent_session="s",
                projects=("proj", "p2"),
                roles={"proj": Role.OPERATOR, "p2": Role.OPERATOR},
            )
            resp = await create_run(
                pool,
                ctx,
                RunCreateRequest(
                    investigation_id=other_inv,
                    system_id=sys_id,
                    build_profile=_profile(),
                ),
                resolver=provider_resolver(),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_create_non_dict_build_profile_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)
            bad: Any = "nope"
            resp = await create_run(
                pool,
                _ctx(),
                RunCreateRequest(investigation_id=inv_id, system_id=sys_id, build_profile=bad),
                resolver=provider_resolver(),
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM runs")
                n = await cur.fetchone()
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert n is not None and n["n"] == 0

    asyncio.run(_run())


def test_create_bare_url_build_profile_does_not_leak_token(migrated_url: str) -> None:
    # A build_profile carrying a credential-looking value in an unknown field must not appear
    # anywhere in the response — neither in data, detail, nor as a literal "input" key. The
    # error propagates through BuildProfile.parse (include_input=False) → RunCreateError →
    # ToolResponse.failure_from_error; this test asserts the full pipeline is leak-free.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)
            resp = await create_run(
                pool,
                _ctx(),
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=sys_id,
                    build_profile={
                        "schema_version": 1,
                        "kernel_source_ref": "https://PLANTED-TOKEN@h/r",
                    },
                ),
                resolver=provider_resolver(),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"
        serialized = str(resp.model_dump(mode="json"))
        assert "PLANTED-TOKEN" not in serialized
        assert "h/r" not in serialized
        assert '"input"' not in serialized

    asyncio.run(_run())


def test_create_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)
            with pytest.raises(AuthorizationError):
                await _create(pool, _ctx(Role.VIEWER), inv_id, sys_id)

    asyncio.run(_run())


def test_create_missing_investigation_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), str(uuid4()), sys_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_create_concurrent_first_runs_flip_once(migrated_url: str) -> None:
    # Two first-Runs on one open Investigation (distinct ready Systems) -> both created,
    # exactly one open->active audit row (the per-Investigation lock makes the flip
    # exactly-once; distinct Systems keep the System locks from serializing the test).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_a = await _seed_system(pool)
            sys_b = await _seed_system(pool)
            r1, r2 = await asyncio.gather(
                _create(pool, _ctx(), inv_id, sys_a),
                _create(pool, _ctx(), inv_id, sys_b),
            )
            assert {r1.status, r2.status} == {"created"}
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'open->active' "
                    "AND object_id = %s",
                    (inv_id,),
                )
                flip = await cur.fetchone()
        assert flip is not None and flip["n"] == 1

    asyncio.run(_run())


def test_create_blocks_on_held_investigation_lock(migrated_url: str) -> None:
    # Deterministic proof create_run takes the INVESTIGATION lock: hold it externally;
    # create_run acquires SYSTEM, then blocks on INVESTIGATION until release.
    import psycopg

    from kdive.db.locks import LockScope, advisory_xact_lock

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            async with await psycopg.AsyncConnection.connect(migrated_url) as holder:
                async with (
                    holder.transaction(),
                    advisory_xact_lock(holder, LockScope.INVESTIGATION, UUID(inv_id)),
                ):
                    task = asyncio.create_task(_create(pool, _ctx(), inv_id, sys_id))
                    await wait_until_any_backend_waiting(holder, locktype="advisory")
                    assert not task.done()  # blocked on the held INVESTIGATION lock
                resp = await task
            assert resp.status == "created"

    asyncio.run(_run())


# --- runs.create: system reuse (#166, ADR-0070) --------------------------------------


async def _provision_job_count(pool: AsyncConnectionPool) -> int:
    return await _count(
        pool, "SELECT count(*) AS n FROM jobs WHERE kind = %s", (JobKind.PROVISION.value,)
    )


async def _non_terminal_run_count(pool: AsyncConnectionPool, sys_id: str) -> int:
    return await _count(
        pool,
        "SELECT count(*) AS n FROM runs WHERE system_id = %s AND state = ANY(%s)",
        (sys_id, [RunState.CREATED.value, RunState.RUNNING.value]),
    )


def test_reuse_attach_runs_without_a_provision_job(migrated_url: str) -> None:
    # Attaching a Run to a matching ready System enqueues NO provision job (provisioning
    # was always separate); the Run is created and can proceed to build/install/boot.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(
                pool, requested_vcpus=8, requested_memory_gb=16, requested_disk_gb=100
            )
            resp = await _create(pool, _ctx(), inv_id, sys_id)
            assert resp.status == "created"
            provision_jobs = await _provision_job_count(pool)
        assert provision_jobs == 0

    asyncio.run(_run())


def test_reuse_optional_assertion_satisfied_creates(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool, requested_vcpus=8, requested_memory_gb=16, requested_disk_gb=100
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(vcpus=4, memory_gb=8, disk_gb=40),
            )
        assert resp.status == "created"

    asyncio.run(_run())


def test_reuse_rejects_non_positive_sizing_requirement(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool, requested_vcpus=8, requested_memory_gb=16, requested_disk_gb=100
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(vcpus=0),
            )
            n = await _count(pool, "SELECT count(*) AS n FROM runs", ())
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["field"] == "vcpus"
        assert n == 0

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("req_vcpus", "req_memory_gb", "req_disk_gb", "label"),
    [
        (16, None, None, "vcpu_short"),
        (None, 64, None, "memory_short"),
        (None, None, 500, "disk_short"),
    ],
)
def test_reuse_assertion_miss_is_config_error_no_run(
    migrated_url: str,
    req_vcpus: int | None,
    req_memory_gb: int | None,
    req_disk_gb: int | None,
    label: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool, requested_vcpus=8, requested_memory_gb=16, requested_disk_gb=100
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(
                    vcpus=req_vcpus,
                    memory_gb=req_memory_gb,
                    disk_gb=req_disk_gb,
                ),
            )
            n = await _count(pool, "SELECT count(*) AS n FROM runs", ())
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert n == 0  # the Run is not created on an assertion miss

    asyncio.run(_run())


def test_reuse_pcie_assertion_contained_creates(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool,
                pcie_claim=[{"bdf": "0000:01:00.0", "vendor_id": "8086", "device_id": "1572"}],
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(pcie=["8086:1572"]),
            )
        assert resp.status == "created"

    asyncio.run(_run())


def test_reuse_pcie_assertion_missing_device_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool,
                pcie_claim=[{"bdf": "0000:01:00.0", "vendor_id": "8086", "device_id": "1572"}],
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(pcie=["10de:1eb8"]),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_reuse_pcie_class_spec_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool,
                pcie_claim=[{"bdf": "0000:01:00.0", "vendor_id": "8086", "device_id": "1572"}],
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(pcie=["class=02"]),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_reuse_empty_pcie_list_is_a_no_op(migrated_url: str) -> None:
    # require_pcie=[] is "provided but asserts nothing" — must not force a failing match.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)  # no pcie_claim at all
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(pcie=[]),
            )
        assert resp.status == "created"

    asyncio.run(_run())


def test_reuse_omitted_assertion_creates_with_only_preconditions(migrated_url: str) -> None:
    # No require_* at all (self-provisioned attach) — only the 3 preconditions apply.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
        assert resp.status == "created"

    asyncio.run(_run())


def test_reuse_full_custom_profile_only_sizing_assertion(migrated_url: str) -> None:
    # Full-custom System: allocation requested_* NULL, size lives only in the profile.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool, provisioning_profile=_profile_dump_sized(vcpu=8, memory_mb=16384, disk_gb=100)
            )
            ok = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(vcpus=4, memory_gb=8, disk_gb=40),
            )
        assert ok.status == "created"

    asyncio.run(_run())


def test_reuse_full_custom_profile_only_sizing_miss_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool, provisioning_profile=_profile_dump_sized(vcpu=2, memory_mb=2048, disk_gb=10)
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(vcpus=8),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_reuse_terminal_allocation_is_stale_handle(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool, alloc_state=AllocationState.EXPIRED)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
        assert resp.status == "error" and resp.error_category == "stale_handle"
        assert resp.data["current_status"] == "expired"

    asyncio.run(_run())


def test_reuse_lapsed_lease_active_allocation_is_stale_handle(migrated_url: str) -> None:
    # ACTIVE allocation whose lease window already elapsed (the orphan-reaping window,
    # ADR-0070): seed a PAST lease_expiry deterministically — do not sleep.
    async def _run() -> None:
        past = datetime(2020, 1, 1, tzinfo=UTC)
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool, alloc_state=AllocationState.ACTIVE, lease_expiry=past)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
        assert resp.status == "error" and resp.error_category == "stale_handle"

    asyncio.run(_run())


def test_reuse_precondition_beats_assertion_miss(migrated_url: str) -> None:
    # A System that BOTH fails an assertion (too small) AND has a terminal alloc returns
    # the precondition error (stale_handle), not the sizing error — no sizing leak.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool,
                alloc_state=AllocationState.EXPIRED,
                requested_vcpus=2,
                requested_memory_gb=2,
                requested_disk_gb=10,
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(vcpus=99),
            )
        assert resp.status == "error" and resp.error_category == "stale_handle"

    asyncio.run(_run())


def test_reuse_system_with_live_run_is_transport_conflict(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            first = await _create(pool, _ctx(), inv_id, sys_id)
            assert first.status == "created"  # holds the System (non-terminal Run)
            second = await _create(pool, _ctx(), inv_id, sys_id)
        assert second.status == "error" and second.error_category == "transport_conflict"

    asyncio.run(_run())


def test_reuse_concurrent_creates_one_wins_other_transport_conflict(migrated_url: str) -> None:
    # Two concurrent runs.create on ONE System: the per-System/per-Allocation lock
    # serializes them, so exactly one is created and the other is transport_conflict.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            r1, r2 = await asyncio.gather(
                _create(pool, _ctx(), inv_id, sys_id),
                _create(pool, _ctx(), inv_id, sys_id),
            )
            statuses = sorted([r1.status, r2.status])
            categories = {r.error_category for r in (r1, r2) if r.status == "error"}
            n_runs = await _count(
                pool, "SELECT count(*) AS n FROM runs WHERE system_id = %s", (sys_id,)
            )
        assert statuses == ["created", "error"]
        assert categories == {"transport_conflict"}
        assert n_runs == 1

    asyncio.run(_run())


def test_reuse_does_not_deadlock_against_release_under_lock_order(migrated_url: str) -> None:
    # The corrected lock order is ALLOCATION -> SYSTEM -> INVESTIGATION (ALLOCATION first,
    # per the global PROJECT<RESOURCE<ALLOCATION<SYSTEM order). allocations.release holds
    # PROJECT->ALLOCATION; an external holder of the ALLOCATION lock must block create_run
    # at its FIRST lock (ALLOCATION) — proving create_run takes ALLOCATION before SYSTEM,
    # so it cannot form a SYSTEM<->ALLOCATION cycle with release / the reconciler sweep.
    import psycopg

    from kdive.db.locks import LockScope, advisory_xact_lock

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT allocation_id FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
            assert row is not None
            alloc_id = row["allocation_id"]
            async with await psycopg.AsyncConnection.connect(migrated_url) as holder:
                async with (
                    holder.transaction(),
                    advisory_xact_lock(holder, LockScope.ALLOCATION, alloc_id),
                ):
                    task = asyncio.create_task(_create(pool, _ctx(), inv_id, sys_id))
                    await wait_until_any_backend_waiting(holder, locktype="advisory")
                    assert not task.done()  # blocked on the held ALLOCATION lock (acquired first)
                resp = await task
            assert resp.status == "created"

    asyncio.run(_run())


# --- shared build fixtures + helpers -------------------------------------------------

_VALID_BUILD: dict[str, Any] = {"schema_version": 1}


async def _count(pool: AsyncConnectionPool, query: LiteralString, params: tuple[Any, ...]) -> int:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        row = await cur.fetchone()
    return 0 if row is None else int(row["n"])


async def _system_id_of(pool: AsyncConnectionPool, run_id: str) -> str:
    """Resolve the System a Run is bound to (the console artifact is System-owned)."""
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT system_id FROM runs WHERE id = %s", (run_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row["system_id"])


# --- build-job fixtures --------------------------------------------------------------

WORKER_LOCAL_ID = "00000000-0000-0000-0000-0000000000c0"


async def _run_count_on_system(pool: AsyncConnectionPool, system_id: str) -> int:
    return await _count(pool, "SELECT count(*) AS n FROM runs WHERE system_id = %s", (system_id,))


def test_create_external_run_succeeds(migrated_url: str) -> None:
    # Every profile is the flat external-upload profile; create inserts a CREATED run.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id, profile=copy.deepcopy(_VALID_BUILD))
            nruns = await _run_count_on_system(pool, sys_id)
        assert resp.status == "created"
        assert nruns == 1

    asyncio.run(_run())


def test_create_second_run_on_live_system_conflicts(migrated_url: str) -> None:
    # A System that already has a non-terminal run rejects a second create with transport_conflict.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            first = await _create(pool, _ctx(), inv_id, sys_id, profile=copy.deepcopy(_VALID_BUILD))
            assert first.status == "created"
            resp = await _create(pool, _ctx(), inv_id, sys_id, profile=copy.deepcopy(_VALID_BUILD))
        assert resp.status == "error" and resp.error_category == "transport_conflict"

    asyncio.run(_run())


# --- build-job seeding helpers (build worker retired; cancel tests still exercise inert
# BUILD-kind jobs) --------------------------------------------------------------------

from kdive.jobs import queue  # noqa: E402
from kdive.jobs.models import HandlerRegistry  # noqa: E402
from kdive.jobs.payloads import (  # noqa: E402
    BuildPayload,
    InstallPayload,
    PayloadValidationError,
    RunPayload,
)


async def _enqueue_build_job(pool: AsyncConnectionPool, run_id: str) -> Job:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
            "VALUES (%s, %s, 'queued', 3, %s, %s) RETURNING *",
            (
                JobKind.BUILD.value,
                Jsonb(
                    BuildPayload(run_id=run_id, build_host_id=str(WORKER_LOCAL_ID)).model_dump(
                        mode="json", exclude_none=True
                    )
                ),
                Jsonb({"principal": "user-1", "agent_session": "s", "project": "proj"}),
                f"{run_id}:build",
            ),
        )
        row = await cur.fetchone()
    assert row is not None
    return Job.model_validate(row)


async def _seed_running_run(pool: AsyncConnectionPool) -> str:
    """A Run admitted for build (created → running) with a valid profile."""
    run_id = await _seed_run(
        pool, state=RunState.CREATED, build_profile=copy.deepcopy(_VALID_BUILD)
    )
    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET state='running' WHERE id=%s", (run_id,))
    return run_id


async def _build_job_for(conn: AsyncConnection, run_id: str) -> Job:
    """Fetch the enqueued build job by its dedup key (no dequeue — no attempt charge)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM jobs WHERE dedup_key=%s", (f"{run_id}:build",))
        row = await cur.fetchone()
    assert row is not None
    return Job.model_validate(row)


# --- runs.install / runs.boot (install + boot plane, #19) ----------------------------

from kdive.domain.capture import CaptureMethod  # noqa: E402
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy  # noqa: E402
from kdive.providers.ports.lifecycle import (  # noqa: E402
    Booter,
    Installer,
    InstallRequest,
)

_LOCAL_POLICY = LocalLibvirtProfilePolicy()
_SUCCEEDED_BUILD: dict[str, Any] = {
    **_VALID_BUILD,
    "cmdline": "console=ttyS0 crashkernel=256M",
}


class _FakeInstaller:
    """Records install() calls (incl. method/initrd_ref); returns or raises a canned category."""

    def __init__(self, *, error: ErrorCategory | None = None) -> None:
        self.calls: list[InstallRequest] = []
        self._error = error

    def install(self, request: InstallRequest) -> None:
        self.calls.append(request)
        if self._error is not None:
            raise CategorizedError("boom", category=self._error)


class _FakeBooter:
    """Records boot() calls; optionally writes the console during boot, then returns or raises.

    ``on_boot`` models libvirt writing the serial console *during* the boot. For local-libvirt
    the serial ``<log>`` is ``append='off'`` and truncated per power-cycle (ADR-0258), so the
    whole file is this boot's window (the boot handler takes no local slice). It runs before any
    canned error, matching a crash whose oops reaches the console before readiness fails.
    """

    def __init__(
        self,
        *,
        error: ErrorCategory | None = None,
        on_boot: Callable[[UUID], None] | None = None,
    ) -> None:
        self.calls: list[UUID] = []
        self._error = error
        self._on_boot = on_boot

    def boot(self, system_id: UUID) -> None:
        self.calls.append(system_id)
        if self._on_boot is not None:
            self._on_boot(system_id)
        if self._error is not None:
            raise CategorizedError("boom", category=self._error)


def _append_console(tmp_path: Path, data: bytes) -> Callable[[UUID], None]:
    """An ``on_boot`` hook that appends ``data`` to the System's serial log during boot."""

    def _write(system_id: UUID) -> None:
        with (tmp_path / f"{system_id}.log").open("ab") as fh:
            fh.write(data)

    return _write


def _truncating_console(tmp_path: Path, data: bytes) -> Callable[[UUID], None]:
    """An ``on_boot`` hook modeling libvirt's ``append='off'`` serial log (ADR-0258).

    Each power-cycle opens the log truncating (``wb``), so the file holds only this boot's
    ``data`` — the prior boot's bytes are gone from disk, not merely sliced off by an offset.
    """

    def _write(system_id: UUID) -> None:
        (tmp_path / f"{system_id}.log").write_bytes(data)

    return _write


async def _seed_succeeded_run(
    pool: AsyncConnectionPool,
    *,
    build_profile: dict[str, Any] | None = None,
    provisioning_profile: dict[str, Any] | None = None,
) -> str:
    """A built Run: state succeeded, kernel_ref set (the install plane's precondition)."""
    run_id = await _seed_run(
        pool,
        state=RunState.SUCCEEDED,
        build_profile=build_profile
        if build_profile is not None
        else copy.deepcopy(_SUCCEEDED_BUILD),
        provisioning_profile=provisioning_profile,
    )
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET kernel_ref=%s WHERE id=%s", (f"local/runs/{run_id}/kernel", run_id)
        )
    await _seed_build_ledger(
        pool, run_id, cmdline=(build_profile or _SUCCEEDED_BUILD).get("cmdline")
    )
    return run_id


async def _seed_succeeded_run_on_system(pool: AsyncConnectionPool, system_id: str) -> str:
    """A second built Run bound to an existing System (a re-boot of the same System)."""
    inv_id = await _seed_investigation(pool)
    async with pool.connection() as conn:
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                investigation_id=UUID(inv_id),
                system_id=UUID(system_id),
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=RunState.SUCCEEDED,
                build_profile=copy.deepcopy(_SUCCEEDED_BUILD),
                failure_category=None,
            ),
        )
        await conn.execute(
            "UPDATE runs SET kernel_ref=%s WHERE id=%s", (f"local/runs/{run.id}/kernel", run.id)
        )
    await _seed_build_ledger(pool, str(run.id), cmdline=_SUCCEEDED_BUILD.get("cmdline"))
    return str(run.id)


async def _record_install_step(pool: AsyncConnectionPool, run_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'install', 'succeeded', '{}'::jsonb)",
            (run_id,),
        )


async def _set_expected_boot_failure(
    pool: AsyncConnectionPool, run_id: str, pattern: str = "__d_lookup|Oops"
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET expected_boot_failure=%s WHERE id=%s",
            (Jsonb({"kind": "console_crash", "pattern": pattern}), run_id),
        )


async def _seed_build_ledger(
    pool: AsyncConnectionPool, run_id: str, *, cmdline: str | None
) -> None:
    """Record a (run_id, 'build') ledger row, optionally carrying the resolved cmdline."""
    result: dict[str, Any] = {
        "kernel_ref": f"local/runs/{run_id}/kernel",
        "debuginfo_ref": f"local/runs/{run_id}/vmlinux",
        "build_id": "abcdef0123456789",
    }
    if cmdline is not None:
        result["cmdline"] = cmdline
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
            (run_id, Jsonb(result)),
        )


async def _install(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> Any:
    return await install_run(
        pool, ctx, run_id, resolver=provider_resolver(profile_policy=_LOCAL_POLICY)
    )


async def _boot(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> Any:
    return await boot_run(pool, ctx, run_id)


def test_install_succeeded_run_enqueues_no_state_flip(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            resp = await _install(pool, _ctx(), run_id)
            assert resp.status == "queued"
            assert resp.data["run_id"] == run_id
            njobs = await _count(
                pool,
                "SELECT count(*) AS n FROM jobs WHERE kind='install' AND dedup_key=%s",
                (f"{run_id}:install",),
            )
            nstate = await _count(
                pool, "SELECT count(*) AS n FROM runs WHERE id=%s AND state='succeeded'", (run_id,)
            )
            naudit = await _count(
                pool,
                "SELECT count(*) AS n FROM audit_log WHERE tool='runs.install' AND object_id=%s",
                (run_id,),
            )
        assert njobs == 1
        assert nstate == 1  # Run stays succeeded (no flip)
        assert naudit == 1

    asyncio.run(_run())


def test_install_is_idempotent_returns_same_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            r1 = await _install(pool, _ctx(), run_id)
            r2 = await _install(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert r1.object_id == r2.object_id
        assert njobs == 1

    asyncio.run(_run())


def test_install_retries_terminal_failed_step_without_rebuild(migrated_url: str) -> None:
    # A transient install failure dead-letters the step job; re-calling runs.install must recycle
    # it to a fresh queued attempt (no new build), not return the wedged failed job (#603).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            first = await _install(pool, _ctx(), run_id)
            # Dead-letter the step job, as the worker would after exhausting attempts.
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE jobs SET state='failed', attempt=3, "
                    "error_category='transport_failure', "
                    'failure_context=\'{"failure_message": "blip"}\'::jsonb '
                    "WHERE dedup_key=%s",
                    (f"{run_id}:install",),
                )

            retry = await _install(pool, _ctx(), run_id)

            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, attempt, error_category FROM jobs WHERE dedup_key=%s",
                    (f"{run_id}:install",),
                )
                job_row = await cur.fetchone()
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
            nbuild = await _count(pool, "SELECT count(*) AS n FROM jobs WHERE kind='build'", ())
            nstate = await _count(
                pool, "SELECT count(*) AS n FROM runs WHERE id=%s AND state='succeeded'", (run_id,)
            )
        assert retry.object_id == first.object_id  # same Run; same step job recycled in place
        assert retry.status == "queued"
        assert job_row is not None
        assert job_row["state"] == "queued"
        assert job_row["attempt"] == 0
        assert job_row["error_category"] is None
        assert njobs == 1  # recycled in place, no duplicate
        assert nbuild == 0  # no rebuild
        assert nstate == 1  # Run stays succeeded

    asyncio.run(_run())


async def _seed_installed_and_booted(
    pool: AsyncConnectionPool,
    run_id: str,
    *,
    installed_cmdline: str | None,
    installed_crashkernel: str | None = None,
) -> None:
    """Seed a Run whose install + boot steps have succeeded (ADR-0299/0300 re-stage setup).

    Records both ``run_steps`` rows succeeded (install carrying ``installed_cmdline`` +
    ``installed_crashkernel``) and both step jobs succeeded, so a subsequent ``runs.install`` sees a
    settled, previously-booted Run.
    """
    async with pool.connection() as conn:
        install_result: dict[str, Any] = {
            "system_id": str(uuid4()),
            "cmdline": installed_cmdline,
            "crashkernel": installed_crashkernel,
        }
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'install', 'succeeded', %s), (%s, 'boot', 'succeeded', '{}'::jsonb)",
            (run_id, Jsonb(install_result), run_id),
        )
    for kind, step, payload in (
        (
            JobKind.INSTALL,
            "install",
            InstallPayload(
                run_id=run_id, cmdline=installed_cmdline, crashkernel=installed_crashkernel
            ),
        ),
        (JobKind.BOOT, "boot", RunPayload(run_id=run_id)),
    ):
        async with pool.connection() as conn:
            job = await queue.enqueue(
                conn,
                kind,
                payload,
                {"principal": "user-1", "agent_session": "s", "project": "proj"},
                f"{run_id}:{step}",
            )
            await conn.execute(
                "UPDATE jobs SET state='succeeded', result_ref='r' WHERE id=%s", (job.id,)
            )


def test_runs_get_reads_installed_crashkernel_from_ledger(migrated_url: str) -> None:
    # End-to-end: step_progress reads the recorded crashkernel off the install row and runs.get
    # surfaces it (ADR-0300). Proves the DB read path, not just the synthetic StepProgress mapping.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, provisioning_profile=_profile_dump(crashkernel="256M")
            )
            await _seed_installed_and_booted(
                pool, run_id, installed_cmdline=None, installed_crashkernel="512M"
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["installed_crashkernel"] == "512M"

    asyncio.run(_run())


async def _run_step_row_exists(pool: AsyncConnectionPool, run_id: str, step: str) -> bool:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM run_steps WHERE run_id=%s AND step=%s", (run_id, step))
        return await cur.fetchone() is not None


def test_install_rejects_platform_owned_cmdline(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            resp = await install_run(
                pool,
                _ctx(),
                run_id,
                cmdline="root=/dev/sda1 quiet",
                resolver=provider_resolver(profile_policy=_LOCAL_POLICY),
            )
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "cmdline_overrides_platform_args"
        assert resp.data["token"] == "root="

    asyncio.run(_run())


def test_install_rejects_blank_cmdline(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            resp = await install_run(
                pool,
                _ctx(),
                run_id,
                cmdline="   ",
                resolver=provider_resolver(profile_policy=_LOCAL_POLICY),
            )
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "cmdline_blank"

    asyncio.run(_run())


def test_install_enqueues_install_payload_with_cmdline(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            resp = await install_run(
                pool,
                _ctx(),
                run_id,
                cmdline="dhash_entries=1",
                resolver=provider_resolver(profile_policy=_LOCAL_POLICY),
            )
            assert resp.error_category is None
            async with pool.connection() as conn:
                job = await queue.get_by_dedup_key(conn, f"{run_id}:install")
        assert job is not None
        assert job.payload["cmdline"] == "dhash_entries=1"

    asyncio.run(_run())


def test_install_differing_cmdline_restages_install_and_boot(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _seed_installed_and_booted(pool, run_id, installed_cmdline="dhash_entries=1")

            resp = await install_run(
                pool,
                _ctx(),
                run_id,
                cmdline="dhash_entries=2",
                resolver=provider_resolver(profile_policy=_LOCAL_POLICY),
            )
            assert resp.error_category is None
            boot_present = await _run_step_row_exists(pool, run_id, "boot")
            async with pool.connection() as conn:
                install_job = await queue.get_by_dedup_key(conn, f"{run_id}:install")
        assert not boot_present  # boot ledger recycled so runs.boot re-runs
        assert install_job is not None
        assert install_job.state is JobState.QUEUED  # succeeded job recycled
        assert install_job.payload["cmdline"] == "dhash_entries=2"  # new cmdline carried

    asyncio.run(_run())


def test_install_same_cmdline_is_noop(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _seed_installed_and_booted(pool, run_id, installed_cmdline="dhash_entries=1")

            resp = await install_run(
                pool,
                _ctx(),
                run_id,
                cmdline="dhash_entries=1",
                resolver=provider_resolver(profile_policy=_LOCAL_POLICY),
            )
            assert resp.error_category is None
            boot_present = await _run_step_row_exists(pool, run_id, "boot")
            async with pool.connection() as conn:
                install_job = await queue.get_by_dedup_key(conn, f"{run_id}:install")
        assert boot_present  # unchanged: no re-stage
        assert install_job is not None
        assert install_job.state is JobState.SUCCEEDED  # not recycled

    asyncio.run(_run())


def test_install_rejected_while_boot_running(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO run_steps (run_id, step, state, result) "
                    "VALUES (%s, 'install', 'succeeded', '{}'::jsonb), "
                    "(%s, 'boot', 'running', NULL)",
                    (run_id, run_id),
                )
            resp = await install_run(
                pool,
                _ctx(),
                run_id,
                cmdline="dhash_entries=2",
                resolver=provider_resolver(profile_policy=_LOCAL_POLICY),
            )
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "step_in_progress"

    asyncio.run(_run())


def _kdump_run(pool: AsyncConnectionPool) -> Any:
    """A built, kdump-provisioned Run with no build-baked cmdline (isolates crashkernel restage)."""
    return _seed_succeeded_run(
        pool,
        build_profile={"schema_version": 1},
        provisioning_profile=_profile_dump(crashkernel="256M"),
    )


def test_install_accepts_crashkernel_and_enqueues_payload(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _kdump_run(pool)
            resp = await install_run(
                pool,
                _ctx(),
                run_id,
                crashkernel="512M",
                resolver=provider_resolver(profile_policy=_LOCAL_POLICY),
            )
            assert resp.error_category is None
            async with pool.connection() as conn:
                job = await queue.get_by_dedup_key(conn, f"{run_id}:install")
        assert job is not None
        assert job.payload["crashkernel"] == "512M"

    asyncio.run(_run())


def test_install_rejects_crashkernel_on_non_kdump_system(migrated_url: str) -> None:
    # A crashkernel on a console-capture System is rejected synchronously at the boundary (#989).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)  # default profile → CONSOLE, not KDUMP
            resp = await install_run(
                pool,
                _ctx(),
                run_id,
                crashkernel="512M",
                resolver=provider_resolver(profile_policy=_LOCAL_POLICY),
            )
            njobs = await _count(
                pool, "SELECT count(*) AS n FROM jobs WHERE dedup_key=%s", (f"{run_id}:install",)
            )
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "crashkernel_requires_kdump"
        assert resp.data["method"] == "console"
        assert njobs == 0  # rejected before any enqueue

    asyncio.run(_run())


def test_install_rejects_blank_crashkernel(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _kdump_run(pool)
            resp = await install_run(
                pool,
                _ctx(),
                run_id,
                crashkernel="   ",
                resolver=provider_resolver(profile_policy=_LOCAL_POLICY),
            )
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "crashkernel_blank"

    asyncio.run(_run())


def test_install_rejects_malformed_crashkernel(migrated_url: str) -> None:
    # Internal whitespace (cmdline injection), a control char (fails XML render), and a leading
    # crashkernel= prefix all → malformed, synchronously at the boundary.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _kdump_run(pool)
            resolver = provider_resolver(profile_policy=_LOCAL_POLICY)
            spaced = await install_run(
                pool, _ctx(), run_id, crashkernel="512M panic=1", resolver=resolver
            )
            control = await install_run(
                pool, _ctx(), run_id, crashkernel="512M\x00panic", resolver=resolver
            )
            prefixed = await install_run(
                pool, _ctx(), run_id, crashkernel="crashkernel=512M", resolver=resolver
            )
        assert spaced.data["reason"] == "crashkernel_malformed"
        assert control.data["reason"] == "crashkernel_malformed"
        assert prefixed.data["reason"] == "crashkernel_malformed"

    asyncio.run(_run())


def test_install_differing_crashkernel_restages_install_and_boot(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _kdump_run(pool)
            # cmdline matches (both build-baked None), so only the crashkernel drives the re-stage.
            await _seed_installed_and_booted(
                pool, run_id, installed_cmdline=None, installed_crashkernel="256M"
            )
            resp = await install_run(
                pool,
                _ctx(),
                run_id,
                crashkernel="512M",
                resolver=provider_resolver(profile_policy=_LOCAL_POLICY),
            )
            assert resp.error_category is None
            boot_present = await _run_step_row_exists(pool, run_id, "boot")
            async with pool.connection() as conn:
                install_job = await queue.get_by_dedup_key(conn, f"{run_id}:install")
        assert not boot_present  # boot ledger recycled so runs.boot re-runs
        assert install_job is not None
        assert install_job.state is JobState.QUEUED  # succeeded job recycled
        assert install_job.payload["crashkernel"] == "512M"  # new reservation carried

    asyncio.run(_run())


def test_install_same_crashkernel_is_noop(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _kdump_run(pool)
            await _seed_installed_and_booted(
                pool, run_id, installed_cmdline=None, installed_crashkernel="512M"
            )
            resp = await install_run(
                pool,
                _ctx(),
                run_id,
                crashkernel="512M",
                resolver=provider_resolver(profile_policy=_LOCAL_POLICY),
            )
            assert resp.error_category is None
            boot_present = await _run_step_row_exists(pool, run_id, "boot")
            async with pool.connection() as conn:
                install_job = await queue.get_by_dedup_key(conn, f"{run_id}:install")
        assert boot_present  # unchanged: no re-stage
        assert install_job is not None
        assert install_job.state is JobState.SUCCEEDED  # not recycled

    asyncio.run(_run())


def test_install_omit_crashkernel_reverts_to_default(migrated_url: str) -> None:
    # Omitting crashkernel on an already-512M Run reverts the reservation to the default 256M
    # (ADR-0300: each install fully specifies its variant; omit → default anchor).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _kdump_run(pool)
            await _seed_installed_and_booted(
                pool, run_id, installed_cmdline=None, installed_crashkernel="512M"
            )
            resp = await install_run(
                pool, _ctx(), run_id, resolver=provider_resolver(profile_policy=_LOCAL_POLICY)
            )
            assert resp.error_category is None
            boot_present = await _run_step_row_exists(pool, run_id, "boot")
            async with pool.connection() as conn:
                install_job = await queue.get_by_dedup_key(conn, f"{run_id}:install")
        assert not boot_present  # re-staged back to default
        assert install_job is not None
        assert install_job.state is JobState.QUEUED
        assert "crashkernel" not in install_job.payload  # None → default 256M

    asyncio.run(_run())


def test_install_crashkernel_change_reverts_omitted_cmdline(migrated_url: str) -> None:
    # The documented cmdline<->crashkernel coupling (ADR-0300): setting crashkernel while omitting
    # cmdline reverts the cmdline to the build-baked extra as it re-stages.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile={**_VALID_BUILD, "cmdline": "baked=1"},
                provisioning_profile=_profile_dump(crashkernel="256M"),
            )
            # Installed with a non-baked cmdline; a later crashkernel-only install reverts it.
            await _seed_installed_and_booted(
                pool, run_id, installed_cmdline="dhash_entries=9", installed_crashkernel="256M"
            )
            resp = await install_run(
                pool,
                _ctx(),
                run_id,
                crashkernel="512M",
                resolver=provider_resolver(profile_policy=_LOCAL_POLICY),
            )
            assert resp.error_category is None
            async with pool.connection() as conn:
                install_job = await queue.get_by_dedup_key(conn, f"{run_id}:install")
        assert install_job is not None
        assert install_job.state is JobState.QUEUED  # re-staged
        assert install_job.payload["crashkernel"] == "512M"
        assert "cmdline" not in install_job.payload  # omitted → reverts to build-baked "baked=1"

    asyncio.run(_run())


def test_cmdline_default_is_kdump_reserving_for_kdump(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool, build_profile={"schema_version": 1})
            async with pool.connection() as conn:
                run = await RUNS.get(conn, UUID(run_id))
                assert run is not None
                cmdline = await run_steps.cmdline_for(
                    conn, run, CaptureMethod.KDUMP, root_cmdline="root=/dev/vda", arch="x86_64"
                )
            assert "crashkernel=" in cmdline
            assert "root=/dev/vda" in cmdline  # the platform injects the root device

    asyncio.run(_run())


def test_cmdline_default_omits_crashkernel_for_non_kdump(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool, build_profile={"schema_version": 1})
            async with pool.connection() as conn:
                run = await RUNS.get(conn, UUID(run_id))
                assert run is not None
                cmdline = await run_steps.cmdline_for(
                    conn, run, CaptureMethod.CONSOLE, root_cmdline="root=/dev/vda", arch="x86_64"
                )
            assert "crashkernel=" not in cmdline
            assert "root=/dev/vda" in cmdline

    asyncio.run(_run())


def test_cmdline_appends_ledger_debug_args_after_the_required_base(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile={"schema_version": 1, "cmdline": "dhash_entries=1"},
            )
            async with pool.connection() as conn:
                run = await RUNS.get(conn, UUID(run_id))
                assert run is not None
                cmdline = await run_steps.cmdline_for(
                    conn, run, CaptureMethod.KDUMP, root_cmdline="root=/dev/vda", arch="x86_64"
                )
            # The platform-required args lead; the agent's debug args are appended after them.
            assert cmdline == "console=ttyS0 root=/dev/vda crashkernel=256M dhash_entries=1"

    asyncio.run(_run())


def test_install_nonkdump_system_admits_cmdline_without_crashkernel(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, build_profile={**_VALID_BUILD, "cmdline": "dhash_entries=1"}
            )  # bare System (default seed profile) => method console
            resp = await _install(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert resp.status == "queued"
        assert njobs == 1

    asyncio.run(_run())


def test_install_kdump_system_admits_without_agent_crashkernel(migrated_url: str) -> None:
    # The platform injects crashkernel for a kdump System (ADR-0061), so the agent need not
    # supply it — a build whose cmdline carries only debug args still admits.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile={**_VALID_BUILD, "cmdline": "dhash_entries=1"},
                provisioning_profile=_profile_dump(crashkernel="256M"),
            )
            resp = await _install(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert resp.status == "queued"
        assert njobs == 1

    asyncio.run(_run())


def test_runs_get_advertises_the_system_required_cmdline(migrated_url: str) -> None:
    # The agent reads the platform-required args off runs.get and appends its debug args without
    # clobbering root=/console (ADR-0061).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, provisioning_profile=_profile_dump(crashkernel="256M")
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["required_cmdline"] == "console=ttyS0 root=/dev/vda crashkernel=256M"

    asyncio.run(_run())


def test_runs_get_omits_root_for_provider_without_platform_root(migrated_url: str) -> None:
    # A provider whose in-guest bootloader owns the root device (remote-libvirt) advertises no
    # root= — injecting one would override the base image's root=UUID (ADR-0183).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, provisioning_profile=_profile_dump(crashkernel="256M")
            )
            resp = await _get_run(
                pool, _ctx(), run_id, resolver=provider_resolver(platform_root_cmdline=None)
            )
        assert resp.data["required_cmdline"] == "console=ttyS0 crashkernel=256M"  # no root=

    asyncio.run(_run())


def test_install_kdump_system_with_crashkernel_enqueues(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile={**_VALID_BUILD, "cmdline": "console=ttyS0 crashkernel=256M"},
                provisioning_profile=_profile_dump(crashkernel="256M"),
            )
            resp = await _install(pool, _ctx(), run_id)
        assert resp.status == "queued"

    asyncio.run(_run())


@pytest.mark.parametrize("state", [RunState.CREATED, RunState.RUNNING])
def test_install_on_unbuilt_run_is_config_error(migrated_url: str, state: RunState) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=state, build_profile=copy.deepcopy(_SUCCEEDED_BUILD)
            )
            resp = await _install(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == state.value

    asyncio.run(_run())


@pytest.mark.parametrize("state", [RunState.FAILED, RunState.CANCELED])
def test_install_on_terminal_run_is_config_error(migrated_url: str, state: RunState) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=state, build_profile=copy.deepcopy(_SUCCEEDED_BUILD)
            )
            resp = await _install(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == state.value

    asyncio.run(_run())


def test_install_cross_project_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            resp = await _install(pool, _ctx(projects=("other",)), run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_install_malformed_uuid_is_invalid_uuid(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _install(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_uuid"
        assert resp.detail is not None
        assert "run_id" in resp.detail and "not-a-uuid" in resp.detail

    asyncio.run(_run())


def test_boot_malformed_uuid_is_invalid_uuid(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _boot(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_uuid"
        assert resp.detail is not None
        assert "run_id" in resp.detail and "not-a-uuid" in resp.detail

    asyncio.run(_run())


def test_install_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            with pytest.raises(AuthorizationError):
                await _install(pool, _ctx(Role.VIEWER), run_id)

    asyncio.run(_run())


def test_boot_without_install_step_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            resp = await _boot(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs WHERE kind='boot'", ())
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert njobs == 0  # no boot job without a succeeded install step

    asyncio.run(_run())


def test_boot_after_install_step_enqueues(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            resp = await _boot(pool, _ctx(), run_id)
            assert resp.status == "queued"
            again = await _boot(pool, _ctx(), run_id)
            njobs = await _count(
                pool,
                "SELECT count(*) AS n FROM jobs WHERE kind='boot' AND dedup_key=%s",
                (f"{run_id}:boot",),
            )
        assert resp.object_id == again.object_id  # idempotent
        assert njobs == 1

    asyncio.run(_run())


def test_boot_fresh_marks_not_replayed(migrated_url: str) -> None:
    # A first boot enqueues fresh work, so the envelope marks replayed=false (#1063).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            resp = await _boot(pool, _ctx(), run_id)
        assert resp.status == "queued"
        assert resp.data["replayed"] is False

    asyncio.run(_run())


def test_boot_repeat_on_succeeded_boot_marks_replayed(migrated_url: str) -> None:
    # A repeat boot on a Run whose boot already succeeded returns the prior job unchanged and
    # marks replayed=true, so a wedged-guest no-op is visibly distinct from a fresh boot (#1063).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _seed_installed_and_booted(pool, run_id, installed_cmdline=None)
            resp = await _boot(pool, _ctx(), run_id)
            njobs = await _count(
                pool,
                "SELECT count(*) AS n FROM jobs WHERE kind='boot' AND dedup_key=%s",
                (f"{run_id}:boot",),
            )
        assert resp.status == "succeeded"  # prior terminal job returned unchanged
        assert resp.data["replayed"] is True
        assert njobs == 1  # no fresh boot enqueued

    asyncio.run(_run())


def test_boot_repeat_before_worker_claim_marks_replayed(migrated_url: str) -> None:
    # Regression: the boot run_steps row is written only when a worker CLAIMS the job, so a boot
    # that is enqueued (queued) but not yet claimed has no row. A second runs.boot in that window
    # dedups to the queued job unchanged and must report replayed=true — a row-presence proxy for
    # the marker would wrongly report replayed=false here (#1063).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            first = await _boot(pool, _ctx(), run_id)
            assert first.data["replayed"] is False  # fresh enqueue, no row yet
            again = await _boot(pool, _ctx(), run_id)  # worker has not claimed → still no row
            njobs = await _count(
                pool,
                "SELECT count(*) AS n FROM jobs WHERE kind='boot' AND dedup_key=%s",
                (f"{run_id}:boot",),
            )
        assert again.object_id == first.object_id  # deduped to the same queued job
        assert again.status == "queued"
        assert again.data["replayed"] is True  # no fresh boot enqueued
        assert njobs == 1

    asyncio.run(_run())


def test_boot_force_recycles_succeeded_boot(migrated_url: str) -> None:
    # force=true recycles the settled boot step so a fresh boot runs without a re-stage: the
    # succeeded boot job resets in place to a fresh queued attempt, marked replayed=false (#1063).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _seed_installed_and_booted(pool, run_id, installed_cmdline=None)
            resp = await boot_run(pool, _ctx(), run_id, force=True)
            boot_present = await _run_step_row_exists(pool, run_id, "boot")
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, attempt, error_category FROM jobs WHERE dedup_key=%s",
                    (f"{run_id}:boot",),
                )
                job_row = await cur.fetchone()
            njobs = await _count(
                pool,
                "SELECT count(*) AS n FROM jobs WHERE kind='boot' AND dedup_key=%s",
                (f"{run_id}:boot",),
            )
        assert resp.status == "queued"
        assert resp.data["replayed"] is False
        assert not boot_present  # boot ledger row recycled so the worker re-runs the boot
        assert job_row is not None
        assert job_row["state"] == "queued"  # succeeded job reset in place
        assert job_row["attempt"] == 0
        assert job_row["error_category"] is None
        assert njobs == 1  # recycled in place, no duplicate

    asyncio.run(_run())


def test_boot_force_rejected_while_boot_running(migrated_url: str) -> None:
    # force must not recycle an in-flight boot: a running boot step is rejected step_in_progress,
    # mirroring the runs.install re-stage guard (#1063).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO run_steps (run_id, step, state, result) "
                    "VALUES (%s, 'boot', 'running', NULL)",
                    (run_id,),
                )
            resp = await boot_run(pool, _ctx(), run_id, force=True)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs WHERE kind='boot'", ())
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "step_in_progress"
        assert njobs == 0  # no boot job enqueued or recycled

    asyncio.run(_run())


def test_boot_force_on_never_booted_enqueues_fresh(migrated_url: str) -> None:
    # force on a Run that was installed but never booted is a fresh boot, same as force=false:
    # nothing to recycle, replayed=false (#1063).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            resp = await boot_run(pool, _ctx(), run_id, force=True)
            njobs = await _count(
                pool,
                "SELECT count(*) AS n FROM jobs WHERE kind='boot' AND dedup_key=%s",
                (f"{run_id}:boot",),
            )
        assert resp.status == "queued"
        assert resp.data["replayed"] is False
        assert njobs == 1

    asyncio.run(_run())


def test_install_envelope_omits_replayed_marker(migrated_url: str) -> None:
    # The replayed marker is boot-only: the runs.install envelope carries no replayed key (#1063).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            resp = await _install(pool, _ctx(), run_id)
        assert resp.error_category is None
        assert "replayed" not in resp.data

    asyncio.run(_run())


@pytest.mark.parametrize("state", [RunState.CREATED, RunState.FAILED])
def test_boot_on_non_succeeded_run_is_config_error(migrated_url: str, state: RunState) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=state, build_profile=copy.deepcopy(_SUCCEEDED_BUILD)
            )
            resp = await _boot(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_boot_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            with pytest.raises(AuthorizationError):
                await _boot(pool, _ctx(Role.VIEWER), run_id)

    asyncio.run(_run())


# --- install_handler / boot_handler (the worker) -------------------------------------


async def _enqueue_job(
    pool: AsyncConnectionPool,
    kind: JobKind,
    run_id: str,
    step: str,
    *,
    cmdline: str | None = None,
    crashkernel: str | None = None,
) -> Job:
    payload: RunPayload = (
        InstallPayload(run_id=run_id, cmdline=cmdline, crashkernel=crashkernel)
        if kind is JobKind.INSTALL
        else RunPayload(run_id=run_id)
    )
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            kind,
            payload,
            {"principal": "user-1", "agent_session": "s", "project": "proj"},
            f"{run_id}:{step}",
        )


async def _install_step_cmdline(pool: AsyncConnectionPool, run_id: str) -> object:
    """Read the recorded applied cmdline from the install step ledger row."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT result FROM run_steps WHERE run_id = %s AND step = 'install'", (run_id,)
        )
        row = await cur.fetchone()
        assert row is not None
        return row[0].get("cmdline")


async def _install_step_crashkernel(pool: AsyncConnectionPool, run_id: str) -> object:
    """Read the recorded applied crashkernel reservation from the install step ledger row (#989)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT result FROM run_steps WHERE run_id = %s AND step = 'install'", (run_id,)
        )
        row = await cur.fetchone()
        assert row is not None
        return row[0].get("crashkernel")


def test_install_handler_applies_and_records_crashkernel(migrated_url: str) -> None:
    # A kdump System's install honors the per-install crashkernel (ADR-0300): the composed cmdline
    # carries crashkernel=512M (not the default 256M) and the value is recorded on the install step.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile={**_VALID_BUILD, "cmdline": "dhash_entries=1"},
                provisioning_profile=_profile_dump(crashkernel="256M"),
            )
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install", crashkernel="512M")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(
                    conn,
                    job,
                    resolver=provider_resolver(installer=installer, profile_policy=_LOCAL_POLICY),
                )
            assert "crashkernel=512M" in installer.calls[0].cmdline
            assert "crashkernel=256M" not in installer.calls[0].cmdline
            assert await _install_step_crashkernel(pool, run_id) == "512M"

    asyncio.run(_run())


def test_install_handler_records_no_crashkernel_when_default(migrated_url: str) -> None:
    # Omitting crashkernel boots the default 256M and records null (the default is in force).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile={**_VALID_BUILD, "cmdline": "dhash_entries=1"},
                provisioning_profile=_profile_dump(crashkernel="256M"),
            )
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(
                    conn,
                    job,
                    resolver=provider_resolver(installer=installer, profile_policy=_LOCAL_POLICY),
                )
            assert "crashkernel=256M" in installer.calls[0].cmdline
            assert await _install_step_crashkernel(pool, run_id) is None

    asyncio.run(_run())


def test_install_handler_rejects_crashkernel_on_non_kdump_system(migrated_url: str) -> None:
    # Backstop (ADR-0300): a crashkernel payload on a non-kdump System fails the job loudly rather
    # than silently dropping the reservation. The tool boundary rejects this earlier; the handler
    # covers a hand-crafted payload or an accept-then-reprovision skew.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)  # default profile → CONSOLE, not KDUMP
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install", crashkernel="512M")
            installer = _FakeInstaller()
            with pytest.raises(CategorizedError) as exc:
                async with pool.connection() as conn:
                    await runs_handlers.install_handler(
                        conn,
                        job,
                        resolver=provider_resolver(
                            installer=installer, profile_policy=_LOCAL_POLICY
                        ),
                    )
            assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert exc.value.details["reason"] == "crashkernel_requires_kdump"
            assert len(installer.calls) == 0  # never staged

    asyncio.run(_run())


_CRASH_GATE_SUPPORTED = frozenset(
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


def test_install_handler_refuses_crashkernel_when_config_lacks_crash_symbols(
    migrated_url: str,
) -> None:
    # ADR-0318: a kdump System whose uploaded effective_config lacks a required crash symbol
    # (PROC_VMCORE here) refuses the crashkernel install with a categorized, symbol-naming reason.
    from unittest.mock import patch

    from kdive.kernel_config.parse import KernelConfig

    missing = KernelConfig(_CRASH_GATE_SUPPORTED - {"PROC_VMCORE"})

    async def _fake_load(conn: Any, run_id: Any, *, store_factory: Any = None) -> KernelConfig:
        return missing

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile={**_VALID_BUILD, "cmdline": "dhash_entries=1"},
                provisioning_profile=_profile_dump(crashkernel="256M"),
            )
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install", crashkernel="512M")
            installer = _FakeInstaller()
            with (
                patch("kdive.kernel_config.gate.load_effective_config", _fake_load),
                pytest.raises(CategorizedError) as exc,
            ):
                async with pool.connection() as conn:
                    await runs_handlers.install_handler(
                        conn,
                        job,
                        resolver=provider_resolver(
                            installer=installer, profile_policy=_LOCAL_POLICY
                        ),
                    )
            assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert exc.value.details["reason"] == "kernel_missing_crash_config"
            assert "PROC_VMCORE" in cast(list[str], exc.value.details["missing"])
            assert len(installer.calls) == 0  # never staged

    asyncio.run(_run())


def test_install_handler_arms_crashkernel_when_config_supports_it(migrated_url: str) -> None:
    # A supported config does not block the crashkernel install (the gate keys on gate_required,
    # so a KASLR-off config with the full crash set still arms).
    from unittest.mock import patch

    from kdive.kernel_config.parse import KernelConfig

    supported = KernelConfig(_CRASH_GATE_SUPPORTED)  # no RANDOMIZE_BASE — still supported

    async def _fake_load(conn: Any, run_id: Any, *, store_factory: Any = None) -> KernelConfig:
        return supported

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile={**_VALID_BUILD, "cmdline": "dhash_entries=1"},
                provisioning_profile=_profile_dump(crashkernel="256M"),
            )
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install", crashkernel="512M")
            installer = _FakeInstaller()
            with patch("kdive.kernel_config.gate.load_effective_config", _fake_load):
                async with pool.connection() as conn:
                    await runs_handlers.install_handler(
                        conn,
                        job,
                        resolver=provider_resolver(
                            installer=installer, profile_policy=_LOCAL_POLICY
                        ),
                    )
            assert len(installer.calls) == 1
            assert "crashkernel=512M" in installer.calls[0].cmdline

    asyncio.run(_run())


def test_install_handler_records_step_run_stays_succeeded(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                result = await runs_handlers.install_handler(
                    conn,
                    job,
                    resolver=provider_resolver(installer=installer, profile_policy=_LOCAL_POLICY),
                )
            assert result == run_id
            assert len(installer.calls) == 1
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='install'",
                (run_id,),
            )
            nstate = await _count(
                pool, "SELECT count(*) AS n FROM runs WHERE id=%s AND state='succeeded'", (run_id,)
            )
        assert nsteps == 1
        assert nstate == 1  # Run unchanged

    asyncio.run(_run())


def test_install_handler_replay_does_not_restage(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(
                    conn,
                    job,
                    resolver=provider_resolver(installer=installer, profile_policy=_LOCAL_POLICY),
                )
            async with pool.connection() as conn:
                await runs_handlers.install_handler(
                    conn,
                    job,
                    resolver=provider_resolver(installer=installer, profile_policy=_LOCAL_POLICY),
                )
        assert len(installer.calls) == 1  # built once

    asyncio.run(_run())


class _SlowInstaller:
    """An installer blocked by the test while the first dispatch owns the step claim."""

    def __init__(self) -> None:
        self.calls: list[UUID] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def install(self, request: InstallRequest) -> None:
        self.calls.append(request.run_id)
        self.entered.set()
        assert self.release.wait(timeout=5), "test did not release the installer"


def test_install_handler_concurrent_dispatch_invokes_once(migrated_url: str) -> None:
    # Two concurrent dispatches of the SAME install job (the queue's at-least-once delivery)
    # on distinct connections: the run_steps running claim serializes them, so the installer
    # runs once and exactly one ledger row is written.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _SlowInstaller()

            async def _dispatch() -> None:
                async with pool.connection() as conn:
                    await runs_handlers.install_handler(
                        conn,
                        job,
                        resolver=provider_resolver(
                            installer=installer, profile_policy=_LOCAL_POLICY
                        ),
                    )

            first = asyncio.create_task(_dispatch())
            assert await asyncio.to_thread(installer.entered.wait, 5)
            second = asyncio.create_task(_dispatch())
            installer.release.set()
            await asyncio.gather(first, second)
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='install'",
                (run_id,),
            )
        assert len(installer.calls) == 1  # the running claim prevents a double redefine
        assert nsteps == 1

    asyncio.run(_run())


def test_install_handler_failure_records_no_step(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller(error=ErrorCategory.INSTALL_FAILURE)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_handlers.install_handler(
                        conn,
                        job,
                        resolver=provider_resolver(
                            installer=installer, profile_policy=_LOCAL_POLICY
                        ),
                    )
            assert caught.value.category is ErrorCategory.INSTALL_FAILURE
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='install'",
                (run_id,),
            )
            nstate = await _count(
                pool, "SELECT count(*) AS n FROM runs WHERE id=%s AND state='succeeded'", (run_id,)
            )
        assert nsteps == 0  # no install ledger row on failure (the build row is expected)
        assert nstate == 1  # Run still succeeded

    asyncio.run(_run())


def test_install_handler_cleanup_failure_preserves_provider_category(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fail_cleanup(*_args: object) -> None:
        raise RuntimeError("cleanup failed")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller(error=ErrorCategory.INSTALL_FAILURE)
            monkeypatch.setattr(run_handler_common, "abandon_run_step", _fail_cleanup)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_handlers.install_handler(
                        conn,
                        job,
                        resolver=provider_resolver(
                            installer=installer, profile_policy=_LOCAL_POLICY
                        ),
                    )

        assert caught.value.category is ErrorCategory.INSTALL_FAILURE

    asyncio.run(_run())


def test_install_handler_missing_kernel_ref_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.SUCCEEDED, build_profile=copy.deepcopy(_SUCCEEDED_BUILD)
            )  # no kernel_ref set
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await runs_handlers.install_handler(
                        conn,
                        job,
                        resolver=provider_resolver(
                            installer=installer, profile_policy=_LOCAL_POLICY
                        ),
                    )
            assert installer.calls == []  # never reached the installer
            nsteps = await _count(
                pool, "SELECT count(*) AS n FROM run_steps WHERE run_id=%s", (run_id,)
            )
        assert nsteps == 0

    asyncio.run(_run())


def test_boot_handler_records_step_run_stays_succeeded(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter()
            async with pool.connection() as conn:
                result = await runs_handlers.boot_handler(
                    conn,
                    job,
                    resolver=provider_resolver(booter=booter),
                    secret_registry=SecretRegistry(),
                )
            assert result == run_id
            assert len(booter.calls) == 1
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='boot'",
                (run_id,),
            )
        assert nsteps == 1

    asyncio.run(_run())


def test_boot_handler_replay_does_not_reboot(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter()
            async with pool.connection() as conn:
                await runs_handlers.boot_handler(
                    conn,
                    job,
                    resolver=provider_resolver(booter=booter),
                    secret_registry=SecretRegistry(),
                )
            async with pool.connection() as conn:
                await runs_handlers.boot_handler(
                    conn,
                    job,
                    resolver=provider_resolver(booter=booter),
                    secret_registry=SecretRegistry(),
                )
        assert len(booter.calls) == 1

    asyncio.run(_run())


@pytest.mark.parametrize("category", [ErrorCategory.BOOT_TIMEOUT, ErrorCategory.READINESS_FAILURE])
def test_boot_handler_failure_records_no_step(migrated_url: str, category: ErrorCategory) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(error=category)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_handlers.boot_handler(
                        conn,
                        job,
                        resolver=provider_resolver(booter=booter),
                        secret_registry=SecretRegistry(),
                    )
            assert caught.value.category is category
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='boot'",
                (run_id,),
            )
        assert nsteps == 0  # no ledger row on failure

    asyncio.run(_run())


def test_boot_handler_cleanup_failure_preserves_provider_category(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fail_cleanup(*_args: object) -> None:
        raise RuntimeError("cleanup failed")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(error=ErrorCategory.BOOT_TIMEOUT)
            monkeypatch.setattr(run_handler_common, "abandon_run_step", _fail_cleanup)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_handlers.boot_handler(
                        conn,
                        job,
                        resolver=provider_resolver(booter=booter),
                        secret_registry=SecretRegistry(),
                    )

        assert caught.value.category is ErrorCategory.BOOT_TIMEOUT

    asyncio.run(_run())


def test_register_handlers_binds_install_and_boot() -> None:
    registry = HandlerRegistry()
    runs_handlers.register_handlers(
        registry,
        ports=runs_handlers.RunHandlerPorts(
            resolver=provider_resolver(
                installer=_FakeInstaller(),
                booter=_FakeBooter(),
                profile_policy=_LOCAL_POLICY,
            ),
            secret_registry=SecretRegistry(),
        ),
    )
    assert registry.get(JobKind.INSTALL) is not None
    assert registry.get(JobKind.BOOT) is not None


def test_boot_handler_registers_console_on_success(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The clean-boot console is the A/B baseline (the `ls /proc`-ran-without-panic
    # evidence) the feature exists to produce, so registration must fire on success too.
    # A real clean boot's console is non-empty (it prints the readiness marker).
    monkeypatch.setattr(console_evidence, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            # The clean-boot console is written during boot (after the mark), so it falls in
            # this Run's window.
            booter = _FakeBooter(
                on_boot=_append_console(tmp_path, b"[    0.0] KDIVE-BUSYBOX-READY\n")
            )
            async with pool.connection() as conn:
                result = await runs_handlers.boot_handler(
                    conn,
                    job,
                    resolver=provider_resolver(booter=booter),
                    secret_registry=SecretRegistry(),
                    artifact_store=minio_store,
                )
            assert result == run_id
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='boot'",
                (run_id,),
            )
            n = await _count(
                pool,
                "SELECT count(*) AS n FROM artifacts WHERE object_key LIKE %s",
                ("%/console-%",),
            )
        assert nsteps == 1  # boot step recorded succeeded
        assert n == 1  # non-empty console registered on the happy path

    asyncio.run(_run())


def test_boot_handler_registers_console_even_on_failure(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # On a crash the panic fires before readiness, but the oops console IS on disk — so a
    # non-empty console must still be captured even though the boot step raises.
    monkeypatch.setattr(console_evidence, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(
                error=ErrorCategory.BOOT_TIMEOUT,
                on_boot=_append_console(tmp_path, b"Kernel panic - not syncing: __d_lookup\n"),
            )
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await runs_handlers.boot_handler(
                        conn,
                        job,
                        resolver=provider_resolver(booter=booter),
                        secret_registry=SecretRegistry(),
                        artifact_store=minio_store,
                    )
            n = await _count(
                pool,
                "SELECT count(*) AS n FROM artifacts WHERE object_key LIKE %s",
                ("%/console-%",),
            )
        assert n == 1

    asyncio.run(_run())


def test_boot_handler_records_expected_crash_observed(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(console_evidence, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _set_expected_boot_failure(pool, run_id)
            await _record_install_step(pool, run_id)
            sid = await _system_id_of(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(
                error=ErrorCategory.READINESS_FAILURE,
                on_boot=_append_console(tmp_path, b"Kernel panic\nRIP: __d_lookup+0x1\n"),
            )
            async with pool.connection() as conn:
                result = await runs_handlers.boot_handler(
                    conn,
                    job,
                    resolver=provider_resolver(booter=booter),
                    secret_registry=SecretRegistry(),
                    artifact_store=minio_store,
                )
            assert result == run_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, result FROM run_steps WHERE run_id=%s AND step='boot'",
                    (run_id,),
                )
                step = await cur.fetchone()
                await cur.execute("SELECT state FROM systems WHERE id=%s", (sid,))
                system = await cur.fetchone()
        assert step is not None
        assert step["state"] == "succeeded"
        assert step["result"]["boot_outcome"] == "expected_crash_observed"
        assert step["result"]["expectation_matched"] is True
        assert step["result"]["evidence_kind"] == "console"
        assert step["result"]["evidence_artifact_id"]
        assert system is not None
        assert system["state"] == "ready"

    asyncio.run(_run())


def test_expected_crash_observed_system_can_host_next_run(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(console_evidence, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _set_expected_boot_failure(pool, run_id)
            await _record_install_step(pool, run_id)
            sys_id = await _system_id_of(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(
                error=ErrorCategory.READINESS_FAILURE,
                on_boot=_append_console(tmp_path, b"Kernel panic\nRIP: __d_lookup+0x1\n"),
            )
            async with pool.connection() as conn:
                await runs_handlers.boot_handler(
                    conn,
                    job,
                    resolver=provider_resolver(booter=booter),
                    secret_registry=SecretRegistry(),
                    artifact_store=minio_store,
                )

            inv_id = await _seed_investigation(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id)

            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id=%s", (sys_id,))
                system = await cur.fetchone()
        assert resp.status == "created"
        assert system is not None
        assert system["state"] == "ready"

    asyncio.run(_run())


def test_boot_handler_expected_crash_requires_matching_console(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(console_evidence, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _set_expected_boot_failure(pool, run_id, pattern="__d_lookup")
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(
                error=ErrorCategory.READINESS_FAILURE,
                on_boot=_append_console(tmp_path, b"Kernel panic\nRIP: other_symbol\n"),
            )
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_handlers.boot_handler(
                        conn,
                        job,
                        resolver=provider_resolver(booter=booter),
                        secret_registry=SecretRegistry(),
                        artifact_store=minio_store,
                    )
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='boot'",
                (run_id,),
            )
        assert caught.value.category is ErrorCategory.READINESS_FAILURE
        assert nsteps == 0

    asyncio.run(_run())


def test_boot_handler_skips_empty_console(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # An empty/unreadable console means capture FAILED (a real boot's console is non-empty).
    # Registering empty bytes as an `available` artifact would be indistinguishable from a
    # crash-free console and could drive a false "fixed" A/B verdict, so it must NOT register.
    monkeypatch.setattr(console_evidence, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter()  # clean success, but no console file was written
            async with pool.connection() as conn:
                result = await runs_handlers.boot_handler(
                    conn,
                    job,
                    resolver=provider_resolver(booter=booter),
                    secret_registry=SecretRegistry(),
                    artifact_store=minio_store,
                )
            assert result == run_id
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='boot'",
                (run_id,),
            )
            n = await _count(
                pool,
                "SELECT count(*) AS n FROM artifacts WHERE object_key LIKE %s",
                ("%/console-%",),
            )
        assert nsteps == 1  # boot itself succeeded
        assert n == 0  # but an empty console capture registers nothing

    asyncio.run(_run())


def test_boot_handler_preserves_console_read_failure(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def fail_read_console_log(_path: Path) -> bytes:
        raise CategorizedError(
            "failed to read console log",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={
                "operation": "read_console_log",
                "path": "/var/lib/kdive/console/example.log",
                "error": "PermissionError",
            },
        )

    monkeypatch.setattr(console_evidence, "read_console_log", fail_read_console_log)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter()
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_handlers.boot_handler(
                        conn,
                        job,
                        resolver=provider_resolver(booter=booter),
                        secret_registry=SecretRegistry(),
                        artifact_store=minio_store,
                    )
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='boot'",
                (run_id,),
            )
        assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert caught.value.details["operation"] == "read_console_log"
        assert nsteps == 0

    asyncio.run(_run())


def test_boot_handler_console_is_readable_via_artifacts(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The registered console artifact must be readable through artifacts_list (ADR-0049 D4).

    The SQL-count tests only verify the row was inserted; this test proves the artifacts
    read surface actually returns the console artifact, closing the behavioral gap.
    """
    from kdive.mcp.tools.catalog.artifacts.reads import artifacts_list

    monkeypatch.setattr(console_evidence, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            system_id = await _system_id_of(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(
                on_boot=_append_console(tmp_path, b"[    0.0] KDIVE-BUSYBOX-READY\n")
            )
            async with pool.connection() as conn:
                result = await runs_handlers.boot_handler(
                    conn,
                    job,
                    resolver=provider_resolver(booter=booter),
                    secret_registry=SecretRegistry(),
                    artifact_store=minio_store,
                )
            assert result == run_id

            # artifacts_list must return the console as a redacted artifact envelope.
            listed = await artifacts_list(pool, _ctx(), system_id=system_id)

        items = listed.items
        assert len(items) == 1
        console = items[0]
        assert console.status == "available"
        assert console.refs is not None
        assert "/console-" in console.refs.get("object", "")

    asyncio.run(_run())


def test_boot_handler_reboot_preserves_prior_run_console(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A second boot of a System keeps the first Run's console intact (ADR-0235, #761).

    The console object key includes the run id, so two boots of the same System write two
    distinct immutable rows. Before the fix the key was System-scoped and the second boot
    overwrote the first Run's bytes, destroying the "before" side of the reproduce→fix→verify
    A/B loop. Each Run's `refs.console` must now resolve to *its own* boot's bytes.

    The two boots run sequentially, matching M0 (a System's Runs boot one at a time). Two Runs
    booting one System *concurrently* is not serialized by boot_handler and is out of scope.
    """
    monkeypatch.setattr(console_evidence, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            # First boot of the System registers a per-Run console row for run1.
            run1 = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run1)
            sid = await _system_id_of(pool, run1)
            job1 = await _enqueue_job(pool, JobKind.BOOT, run1, "boot")
            first_boot = _FakeBooter(
                on_boot=_truncating_console(tmp_path, b"FIRST-BOOT-MARKER ready\n")
            )
            async with pool.connection() as conn:
                await runs_handlers.boot_handler(
                    conn,
                    job1,
                    resolver=provider_resolver(booter=first_boot),
                    secret_registry=SecretRegistry(),
                    artifact_store=minio_store,
                )

            # Second boot of the SAME System (new Run): libvirt's append='off' serial log is
            # truncated on power-cycle (ADR-0258), so run2's log holds only its own bytes, captured
            # whole and written under run2's own per-Run object key — run1's row is untouched.
            run2 = await _seed_succeeded_run_on_system(pool, sid)
            await _record_install_step(pool, run2)
            job2 = await _enqueue_job(pool, JobKind.BOOT, run2, "boot")
            second_boot = _FakeBooter(
                on_boot=_truncating_console(tmp_path, b"SECOND-BOOT-MARKER oops\n")
            )
            async with pool.connection() as conn:
                await runs_handlers.boot_handler(
                    conn,
                    job2,
                    resolver=provider_resolver(booter=second_boot),
                    secret_registry=SecretRegistry(),
                    artifact_store=minio_store,
                )

            n = await _count(
                pool,
                "SELECT count(*) AS n FROM artifacts WHERE object_key LIKE %s",
                ("%/console-%",),
            )
            key1 = f"local/systems/{sid}/console-{run1}"
            key2 = f"local/systems/{sid}/console-{run2}"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT object_key, etag FROM artifacts WHERE object_key = %s", (key1,)
                )
                row1 = await cur.fetchone()
                await cur.execute(
                    "SELECT object_key, etag FROM artifacts WHERE object_key = %s", (key2,)
                )
                row2 = await cur.fetchone()

        assert n == 2  # one immutable console row per Run, never collapsed onto one key
        assert row1 is not None and row2 is not None
        # The first Run's console is preserved — it still resolves to ITS OWN boot's bytes,
        # not the second boot's (the A/B "before" evidence the issue exists to protect).
        first = minio_store.get_artifact(row1["object_key"], row1["etag"])
        assert b"FIRST-BOOT-MARKER" in first.data
        assert b"SECOND-BOOT-MARKER" not in first.data
        second = minio_store.get_artifact(row2["object_key"], row2["etag"])
        assert b"SECOND-BOOT-MARKER" in second.data
        assert b"FIRST-BOOT-MARKER" not in second.data

    asyncio.run(_run())


def _assert_ports() -> None:
    # Structural conformance: the fakes satisfy the realized Protocols (ty enforces; this
    # keeps the import used and documents the contract).
    _i: Installer = _FakeInstaller()
    _b: Booter = _FakeBooter()
    assert _i is not None and _b is not None


def _system_with_profile(profile: dict[str, Any]) -> System:
    return System(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        allocation_id=uuid4(),
        state=SystemState.READY,
        provisioning_profile=profile,
    )


def _profile_dump(**local_libvirt: Any) -> dict[str, Any]:
    """A real ProvisioningProfile.model_dump(by_alias=True) — pins the 'local-libvirt' alias."""
    from kdive.profiles.provisioning import ProvisioningProfile

    section: dict[str, Any] = {"rootfs": {"kind": "local", "path": "/img"}}
    section.update(local_libvirt)
    return ProvisioningProfile.model_validate(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 10,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://git.kernel.org#v6.9",
            "provider": {"local-libvirt": section},
        }
    ).model_dump(by_alias=True)


def _profile_dump_sized(*, vcpu: int, memory_mb: int, disk_gb: int) -> dict[str, Any]:
    """A real provisioning-profile dump with explicit sizing (the full-custom reuse case)."""
    from kdive.profiles.provisioning import ProvisioningProfile

    return ProvisioningProfile.model_validate(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": vcpu,
            "memory_mb": memory_mb,
            "disk_gb": disk_gb,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://git.kernel.org#v6.9",
            "provider": {"local-libvirt": {"rootfs": {"kind": "local", "path": "/img"}}},
        }
    ).model_dump(by_alias=True)


def test_install_method_kdump_when_crashkernel_set() -> None:
    system = _system_with_profile(_profile_dump(crashkernel="256M"))
    assert run_steps.install_method_for(system, _LOCAL_POLICY) is CaptureMethod.KDUMP


def test_install_method_gdbstub_when_flag_set() -> None:
    system = _system_with_profile(_profile_dump(debug={"gdbstub": True}))
    assert run_steps.install_method_for(system, _LOCAL_POLICY) is CaptureMethod.GDBSTUB


def test_install_method_host_dump_when_preserve_on_crash() -> None:
    system = _system_with_profile(_profile_dump(debug={"preserve_on_crash": True}))
    assert run_steps.install_method_for(system, _LOCAL_POLICY) is CaptureMethod.HOST_DUMP


def test_install_method_console_for_bare_system() -> None:
    system = _system_with_profile(_profile_dump())
    assert run_steps.install_method_for(system, _LOCAL_POLICY) is CaptureMethod.CONSOLE


def test_install_method_rejects_partial_profile() -> None:
    system = _system_with_profile({"schema_version": 1})
    with pytest.raises(CategorizedError) as exc:
        run_steps.install_method_for(system, _LOCAL_POLICY)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_install_method_rejects_attribute_spelling() -> None:
    system = _system_with_profile({"provider": {"local_libvirt": {"crashkernel": "256M"}}})
    with pytest.raises(CategorizedError) as exc:
        run_steps.install_method_for(system, _LOCAL_POLICY)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


async def _record_build_ledger(
    pool: AsyncConnectionPool, run_id: str, result: dict[str, Any]
) -> None:
    # Upsert: a succeeded-run seed (`_seed_build_ledger`) already inserts a build row, so a test
    # that needs a specific build result (e.g. an initrd_ref) overwrites it rather than no-op'ing.
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) "
            "ON CONFLICT (run_id, step) DO UPDATE SET result = EXCLUDED.result",
            (run_id, Jsonb(result)),
        )


def test_install_handler_forwards_console_method_for_bare_system(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)  # bare System => console
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(
                    conn,
                    job,
                    resolver=provider_resolver(installer=installer, profile_policy=_LOCAL_POLICY),
                )
        assert installer.calls[0].method is CaptureMethod.CONSOLE
        assert installer.calls[0].initrd_ref is None  # no initrd

    asyncio.run(_run())


def test_install_handler_forwards_host_dump_for_preserve_on_crash(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, provisioning_profile=_profile_dump(debug={"preserve_on_crash": True})
            )
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(
                    conn,
                    job,
                    resolver=provider_resolver(installer=installer, profile_policy=_LOCAL_POLICY),
                )
        assert installer.calls[0].method is CaptureMethod.HOST_DUMP

    asyncio.run(_run())


def test_install_handler_forwards_initrd_ref_from_build_ledger(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_build_ledger(
                pool, run_id, {"kernel_ref": "k", "initrd_ref": "local/runs/x/initrd"}
            )
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(
                    conn,
                    job,
                    resolver=provider_resolver(installer=installer, profile_policy=_LOCAL_POLICY),
                )
        assert installer.calls[0].initrd_ref == "local/runs/x/initrd"

    asyncio.run(_run())


def test_install_handler_no_initrd_when_ledger_initrd_blank(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_build_ledger(pool, run_id, {"kernel_ref": "k", "initrd_ref": ""})
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(
                    conn,
                    job,
                    resolver=provider_resolver(installer=installer, profile_policy=_LOCAL_POLICY),
                )
        assert installer.calls[0].initrd_ref is None

    asyncio.run(_run())


def test_install_handler_forwards_ledger_cmdline_to_installer(migrated_url: str) -> None:
    """The dhash_entries=1 trigger recorded in the build ledger reaches install() (#128)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, build_profile={**_VALID_BUILD, "cmdline": "dhash_entries=1"}
            )  # bare System => console method; the debug arg is appended to the required base
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(
                    conn,
                    job,
                    resolver=provider_resolver(installer=installer, profile_policy=_LOCAL_POLICY),
                )
        assert installer.calls[0].cmdline == "console=ttyS0 root=/dev/vda dhash_entries=1"

    asyncio.run(_run())


def test_install_handler_forwards_default_cmdline_when_ledger_has_none(migrated_url: str) -> None:
    """A succeeded run with no ledger cmdline installs the method default, not a stale value."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile=copy.deepcopy(_VALID_BUILD),  # no cmdline key
            )
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(
                    conn,
                    job,
                    resolver=provider_resolver(installer=installer, profile_policy=_LOCAL_POLICY),
                )
        assert installer.calls[0].cmdline == "console=ttyS0 root=/dev/vda"  # required base only

    asyncio.run(_run())


def test_install_handler_payload_cmdline_overrides_ledger(migrated_url: str) -> None:
    """The install payload cmdline (ADR-0299) replaces the build-baked extra, not appends it."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, build_profile={**_VALID_BUILD, "cmdline": "dhash_entries=9"}
            )
            job = await _enqueue_job(
                pool, JobKind.INSTALL, run_id, "install", cmdline="dhash_entries=1"
            )
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(
                    conn,
                    job,
                    resolver=provider_resolver(installer=installer, profile_policy=_LOCAL_POLICY),
                )
            applied = installer.calls[0].cmdline
            # Override replaces the baked extra; the build value never appears.
            assert applied == "console=ttyS0 root=/dev/vda dhash_entries=1"
            assert "dhash_entries=9" not in applied
            assert await _install_step_cmdline(pool, run_id) == "dhash_entries=1"

    asyncio.run(_run())


def test_step_progress_reads_installed_cmdline(migrated_url: str) -> None:
    """step_progress surfaces the applied install cmdline for runs.get read-back (ADR-0299)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool, build_profile=copy.deepcopy(_VALID_BUILD))
            job = await _enqueue_job(
                pool, JobKind.INSTALL, run_id, "install", cmdline="dhash_entries=1"
            )
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(
                    conn,
                    job,
                    resolver=provider_resolver(installer=installer, profile_policy=_LOCAL_POLICY),
                )
            async with pool.connection() as conn:
                progress = await step_progress(conn, UUID(run_id))
        assert progress.installed_cmdline == "dhash_entries=1"

    asyncio.run(_run())


def test_install_handler_rejects_retired_composite_phase_job(migrated_url: str) -> None:
    """The retired build_install_boot kind no longer bypasses exact install payload decoding."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, build_profile={**_VALID_BUILD, "cmdline": "dhash_entries=9"}
            )
            # Historical composite rows carried the composite kind with a bare {run_id} payload.
            phase_job = Job(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                kind=JobKind.BUILD_INSTALL_BOOT,
                payload={"run_id": run_id},
                state=JobState.RUNNING,
                max_attempts=3,
                authorizing={"principal": "user-1", "agent_session": "s", "project": "proj"},
                dedup_key=f"{run_id}:composite",
            )
            async with pool.connection() as conn:
                with pytest.raises(
                    PayloadValidationError,
                    match="InstallPayload does not match build_install_boot payload contract",
                ):
                    await runs_handlers.install_handler(
                        conn,
                        phase_job,
                        resolver=provider_resolver(
                            installer=_FakeInstaller(), profile_policy=_LOCAL_POLICY
                        ),
                    )

    asyncio.run(_run())


def test_install_cmdline_distinguishes_audit_digest(migrated_url: str) -> None:
    """Different install cmdlines produce different audit args_digests (audit integrity).

    The audit stores a one-way args_digest, so the cmdline is not reverse-readable; including it
    ensures a re-stage to a new cmdline is not audited identically to the prior install.
    """

    async def _digest(pool: AsyncConnectionPool, run_id: str, cmdline: str | None) -> str:
        await install_run(
            pool,
            _ctx(),
            run_id,
            cmdline=cmdline,
            resolver=provider_resolver(profile_policy=_LOCAL_POLICY),
        )
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT args_digest FROM audit_log WHERE tool='runs.install' AND object_id=%s",
                (run_id,),
            )
            row = await cur.fetchone()
        assert row is not None
        return row["args_digest"]

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            base = await _digest(pool, await _seed_succeeded_run(pool), None)
            one = await _digest(pool, await _seed_succeeded_run(pool), "dhash_entries=1")
            two = await _digest(pool, await _seed_succeeded_run(pool), "dhash_entries=2")
        assert base != one and one != two and base != two  # distinct digest per cmdline

    asyncio.run(_run())


def test_install_handler_records_build_extra_when_no_override(migrated_url: str) -> None:
    """With no payload override, the recorded applied cmdline is the build-baked extra."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, build_profile={**_VALID_BUILD, "cmdline": "dhash_entries=9"}
            )
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(
                    conn,
                    job,
                    resolver=provider_resolver(installer=installer, profile_policy=_LOCAL_POLICY),
                )
            assert await _install_step_cmdline(pool, run_id) == "dhash_entries=9"

    asyncio.run(_run())


@pytest.mark.parametrize(
    "cmdline",
    ["dhash_entries=1 panic_on_oops=1", "panic_on_oops=1"],
)
def test_install_debug_args_pass_boundary(migrated_url: str, cmdline: str) -> None:
    # The platform injects console/root; agent-supplied debug args carry no crashkernel= and
    # a bare (console) System admits them through runs.install.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, build_profile={**_VALID_BUILD, "cmdline": cmdline}
            )
            resp = await _install(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert resp.status == "queued"
        assert njobs == 1

    asyncio.run(_run())


# --- runs.cancel ---------------------------------------------------------------


async def _run_state(pool: AsyncConnectionPool, run_id: str) -> str:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row["state"])


@pytest.mark.parametrize(
    ("state", "transition"),
    [(RunState.CREATED, "created->canceled"), (RunState.RUNNING, "running->canceled")],
)
def test_cancel_drives_non_terminal_run_canceled(
    migrated_url: str, state: RunState, transition: str
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            if state is RunState.RUNNING:
                async with pool.connection() as conn:
                    await conn.execute("UPDATE runs SET state='running' WHERE id=%s", (run_id,))
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), run_id)
            assert resp.status == "canceled"
            assert resp.error_category is None
            assert resp.suggested_next_actions == ["runs.create"]
            assert await _run_state(pool, run_id) == "canceled"
            n = await _count(
                pool,
                "SELECT count(*) AS n FROM audit_log WHERE transition=%s AND object_id=%s",
                (transition, run_id),
            )
        assert n == 1

    asyncio.run(_run())


def test_cancel_already_canceled_is_idempotent_no_op(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CANCELED)
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), run_id)
            assert resp.status == "canceled"
            assert resp.error_category is None
            n = await _count(
                pool,
                "SELECT count(*) AS n FROM audit_log WHERE tool='runs.cancel' AND object_id=%s",
                (run_id,),
            )
        assert n == 0

    asyncio.run(_run())


@pytest.mark.parametrize("state", [RunState.SUCCEEDED, RunState.FAILED])
def test_cancel_other_terminal_run_conflicts(migrated_url: str, state: RunState) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=state)
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), run_id)
            assert resp.status == "error"
            assert resp.error_category == "conflict"
            assert resp.data["current_status"] == state.value
            assert await _run_state(pool, run_id) == state.value

    asyncio.run(_run())


def test_cancel_frees_system_for_a_new_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            first = await _create(pool, _ctx(), inv_id, sys_id)
            assert first.status == "created"
            blocked = await _create(pool, _ctx(), inv_id, sys_id)
            assert blocked.status == "error"
            assert blocked.error_category == "transport_conflict"
            assert blocked.data["reason"] == "system_has_live_run"
            cancel = await cancel_run(pool, _ctx(Role.OPERATOR), first.object_id)
            assert cancel.status == "canceled"
            again = await _create(pool, _ctx(), inv_id, sys_id)
            assert again.status == "created"

    asyncio.run(_run())


def test_cancel_unknown_run_id_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), str(uuid4()))
            assert resp.status == "error"
            assert resp.error_category == "not_found"

    asyncio.run(_run())


def test_cancel_malformed_run_id_is_invalid_uuid(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), "not-a-uuid")
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            assert resp.data["reason"] == "invalid_uuid"
            assert resp.detail is not None
            assert "run_id" in resp.detail and "not-a-uuid" in resp.detail

    asyncio.run(_run())


def test_cancel_cross_project_run_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED, project="proj")
            resp = await cancel_run(pool, _ctx(Role.OPERATOR, projects=("other",)), run_id)
            assert resp.status == "error"
            assert resp.error_category == "not_found"
            assert await _run_state(pool, run_id) == "created"

    asyncio.run(_run())


def test_cancel_requires_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            with pytest.raises(AuthorizationError):
                await cancel_run(pool, _ctx(Role.VIEWER), run_id)
            assert await _run_state(pool, run_id) == "created"

    asyncio.run(_run())


def test_cancel_cancels_in_flight_build_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            await _enqueue_build_job(pool, run_id)
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), run_id)
            assert resp.status == "canceled"
            async with pool.connection() as conn:
                job = await _build_job_for(conn, run_id)
            assert job.state is JobState.CANCELED
            assert await _run_state(pool, run_id) == "canceled"

    asyncio.run(_run())


def test_cancel_leaves_terminal_build_job_untouched(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            async with pool.connection() as conn:
                await JOBS.update_state(conn, job.id, JobState.RUNNING)
                await JOBS.update_state(conn, job.id, JobState.SUCCEEDED)
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), run_id)
            assert resp.status == "canceled"
            async with pool.connection() as conn:
                refreshed = await _build_job_for(conn, run_id)
            assert refreshed.state is JobState.SUCCEEDED

    asyncio.run(_run())


def test_cancel_running_run_with_running_build_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            async with pool.connection() as conn:
                await JOBS.update_state(conn, job.id, JobState.RUNNING)
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), run_id)
            assert resp.status == "canceled"
            async with pool.connection() as conn:
                refreshed = await _build_job_for(conn, run_id)
            assert refreshed.state is JobState.CANCELED
            assert await _run_state(pool, run_id) == "canceled"

    asyncio.run(_run())


def test_cancel_swallows_build_job_race_to_terminal(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The worker completes a build job via fenced raw SQL holding no per-Run lock, so a job
    # read as `running` can turn terminal before cancel's FOR UPDATE acquires it. Simulate
    # that race: the real row is `succeeded`, but get_by_dedup_key (as cancel sees it) returns
    # a stale `running` snapshot, so JOBS.update_state hits the terminal row and raises
    # IllegalTransition. The cancel must swallow it and still drive the Run to canceled.
    from kdive.mcp.tools.lifecycle.runs import cancel as cancel_mod

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            async with pool.connection() as conn:
                await JOBS.update_state(conn, job.id, JobState.RUNNING)
                await JOBS.update_state(conn, job.id, JobState.SUCCEEDED)
            stale = job.model_copy(update={"state": JobState.RUNNING})

            async def _stale_get(conn: AsyncConnection, dedup_key: str) -> Job:
                return stale

            monkeypatch.setattr(cancel_mod.queue, "get_by_dedup_key", _stale_get)
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), run_id)
            assert resp.status == "canceled"
            assert await _run_state(pool, run_id) == "canceled"
            async with pool.connection() as conn:
                refreshed = await _build_job_for(conn, run_id)
            assert refreshed.state is JobState.SUCCEEDED

    asyncio.run(_run())


def test_run_envelope_surfaces_investigation_build_and_artifacts() -> None:
    inv_id = uuid4()
    run = Run(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        investigation_id=inv_id,
        system_id=None,
        target_kind=ResourceKind.LOCAL_LIBVIRT,
        state=RunState.SUCCEEDED,
        build_profile={"schema_version": 1},
        kernel_ref="s3://bucket/vmlinuz",
        debuginfo_ref="s3://bucket/vmlinux",
    )
    resp = runs_common.envelope_for_run(run)
    assert resp.data["investigation_id"] == str(inv_id)
    assert resp.data["build_source"] == "external"
    assert "build_host" not in resp.data
    assert "build_source_provenance" not in resp.data
    assert resp.refs == {"kernel": "s3://bucket/vmlinuz", "debuginfo": "s3://bucket/vmlinux"}


def test_failed_run_envelope_keeps_investigation_and_artifacts() -> None:
    run = Run(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        investigation_id=uuid4(),
        system_id=None,
        target_kind=ResourceKind.LOCAL_LIBVIRT,
        state=RunState.FAILED,
        build_profile=_profile(),
        failure_category=ErrorCategory.INSTALL_FAILURE,
        kernel_ref="s3://bucket/vmlinuz",
    )
    resp = runs_common.envelope_for_run(run)
    assert resp.status == "error"
    assert "investigation_id" in resp.data
    assert resp.refs == {"kernel": "s3://bucket/vmlinuz"}


def test_get_succeeded_run_surfaces_build_provenance(migrated_url: str) -> None:
    # A SUCCEEDED run whose build step recorded provenance → data["build_provenance"] present
    # with all four fields verbatim, so an agent can trace exactly what was built (#778).
    provenance = {
        "remote": "https://github.com/torvalds/linux",
        "ref": "v6.9",
        "resolved_commit": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",  # pragma: allowlist secret
        "build_host": "build-worker-1",
    }

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(
                pool,
                run_id,
                "build",
                "succeeded",
                {
                    "kernel_ref": f"local/runs/{run_id}/kernel",
                    "debuginfo_ref": f"local/runs/{run_id}/vmlinux",
                    "build_id": "abc123",
                    "build_provenance": provenance,
                },
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["build_provenance"] == provenance

    asyncio.run(_run())


def test_get_succeeded_run_surfaces_warm_tree_dirty_as_native_bool(migrated_url: str) -> None:
    # A warm-tree build records dirty as a native JSON bool (#861, ADR-0263/0265); it must reach
    # data["build_provenance"]["dirty"] as a real bool through the JSON round-trip, not a string.
    provenance = {
        "label": "linux-6.9",
        "resolved_commit": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",  # pragma: allowlist secret
        "dirty": True,
        "tree_sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",  # pragma: allowlist secret
    }

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(
                pool,
                run_id,
                "build",
                "succeeded",
                {
                    "kernel_ref": f"local/runs/{run_id}/kernel",
                    "debuginfo_ref": f"local/runs/{run_id}/vmlinux",
                    "build_id": "abc123",
                    "build_provenance": provenance,
                },
            )
            resp = await get_run(pool, _ctx(), run_id)
        surfaced = resp.data["build_provenance"]
        assert surfaced == provenance
        assert isinstance(surfaced, dict)
        assert surfaced["dirty"] is True

    asyncio.run(_run())


def test_get_succeeded_run_surfaces_dirty_files_list(migrated_url: str) -> None:
    # A warm-tree build records dirty_files as a JSON string array (#938, ADR-0282); it must reach
    # data["build_provenance"]["dirty_files"] as a real list through the DB JSON round-trip, not be
    # dropped by the provenance coercion.
    provenance = {
        "label": "linux-6.9",
        "resolved_commit": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",  # pragma: allowlist secret
        "dirty": True,
        "untracked": False,
        "tree_sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",  # pragma: allowlist secret
        "dirty_files": ["kernel/sched/core.c", "mm/slub.c"],
    }

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(
                pool,
                run_id,
                "build",
                "succeeded",
                {
                    "kernel_ref": f"local/runs/{run_id}/kernel",
                    "debuginfo_ref": f"local/runs/{run_id}/vmlinux",
                    "build_id": "abc123",
                    "build_provenance": provenance,
                },
            )
            resp = await get_run(pool, _ctx(), run_id)
        surfaced = resp.data["build_provenance"]
        assert surfaced == provenance
        assert isinstance(surfaced, dict)
        assert surfaced["dirty_files"] == ["kernel/sched/core.c", "mm/slub.c"]
        assert surfaced["untracked"] is False

    asyncio.run(_run())


def test_get_succeeded_run_omits_build_provenance_key_when_absent(migrated_url: str) -> None:
    # A SUCCEEDED run whose build step recorded no provenance → "build_provenance" key must be
    # entirely absent from data (not present-as-null), so callers can key off its presence (#778).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(
                pool,
                run_id,
                "build",
                "succeeded",
                {
                    "kernel_ref": f"local/runs/{run_id}/kernel",
                    "debuginfo_ref": f"local/runs/{run_id}/vmlinux",
                    "build_id": "abc123",
                },
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert "build_provenance" not in resp.data

    asyncio.run(_run())
