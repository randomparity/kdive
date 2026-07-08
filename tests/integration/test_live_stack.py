"""The phase-structured local-libvirt live-stack spine driver (#100, ADR-0042 §1/§4/§5, ADR-0045).

Drives the full kdive spine — allocate → provision → open-investigation → create-run → build →
install → boot → attach → crash → capture → introspect → release → (reconciler) teardown → report
— over the **live MCP HTTP transport** via the merged harness (``mint_token`` + ``LiveStackClient``)
— each step a tool call under a specific OIDC role token, the async job kinds drained by the real
host ``worker`` + ``reconciler``. The provider-agnostic spine scaffolding (phase naming, drain /
state polling, role tokens, out-of-band DB seeding, audit / ledger / report helpers) lives in
``tests.integration.live_stack.spine`` and is shared with the remote spine; this module keeps the
local-libvirt profile factories, the spine body, the RBAC-negative wire tests, and the
local-libvirt owned-infra teardown check.

Acceptance asserted over the wire / against the stack's Postgres + MinIO: protocol (well-formed
envelopes, JWKS-validated tokens), #1 (redacted vmcore in MinIO), #2 (audit per transition +
force_crash, split by attributing principal — driver vs ``system:reconciler``), #3 (redaction does
not leak through the wire), #5 (``torn_down`` + ``Discovery.list_owned()`` empty), the report phase
(``accounting.report_all_projects`` under a ``platform_auditor`` token, windowed to this run —
ADR-0046), and the RBAC negatives (viewer raised-path; operator force_crash ``authorization_denied``
envelope; project-only token denied the all-projects report).

The shared phase-naming contract has its own non-gated unit tests in
``tests/integration/live_stack/test_spine.py``; the spine + RBAC tests here are
``live_stack``-marked and skip without a stack.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.sizing import AllocationSizing
from kdive.profiles.provisioning import reconcile_profile_sizing
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.harness import (
    LiveStackClient,
    LiveStackToolError,
    OidcIssuer,
)
from tests.integration.live_stack.spine import (
    LOCAL_ALLOCATION_DISK_GB,
    SpinePhaseError,
    assert_audit,
    assert_report,
    await_system_state,
    drain_job,
    mint_role_token,
    ok,
    phase,
    scalar,
    seed_metering,
    system_torn_down,
)
from tests.mcp.json_data import data_mapping, data_str

_GUEST_IMAGE_ENV = "KDIVE_GUEST_IMAGE"
_KERNEL_TREE_ENV = "KDIVE_KERNEL_SRC"
_DATABASE_URL_ENV = "KDIVE_DATABASE_URL"
_PROJECT = "spine-proj"
_AGENT_SESSION = "spine-sess"
_ARTIFACT_NAME = "accounting-report.json"

# Per-family *-kdive-ready rootfs images for the SSH-reachability proof (#956, ADR-0294). Each is a
# distinct artifact; a host with only one family's image proves that family and skips the other.
_FAMILY_IMAGE_ENV = {"debian": "KDIVE_GUEST_IMAGE_DEBIAN", "rhel": "KDIVE_GUEST_IMAGE_RHEL"}
# A throwaway ed25519 public key (public half only; KDIVE never needs the private key to append it).
# Fixed is fine: authorize_ssh_key dedups on the key fingerprint, so a re-run is idempotent.
_REACHABILITY_PUBKEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINEaCXYW3lMCAliMtbmJIW1X7VbPd51hhXaFT5VOBwSU "
    "kdive-956-reachability-e2e"
)

_MAX_SCRIPT_BYTES = 256 * 1024
# A real drgn script only a live kernel can answer: `drgn -k -q <file>` provides `prog` (the live
# drgn.Program). The marker makes the genuine in-guest output unmistakable in the returned envelope.
_PROOF_SCRIPT = """\
from drgn.helpers.linux.pid import for_each_task

release = prog["init_uts_ns"].name.release.string_().decode()
init_comm = prog["init_task"].comm.string_().decode()
ntasks = sum(1 for _ in for_each_task(prog))
print(f"DRGN_LIVE_PROOF release={release} init_comm={init_comm} ntasks={ntasks}")
"""
# Over the 256 KiB inbound cap: the tool rejects it as configuration_error before any guest send.
_OVERSIZE_SCRIPT = "# pad\n" + ("x" * (_MAX_SCRIPT_BYTES + 16))


# --- preflight helpers ----------------------------------------------------------------------


def _spine_preflight() -> tuple[OidcIssuer, str, str]:
    """Resolve issuer + stack URL + DB URL, or skip with the exact fix (ADR-0035 §4)."""
    image = os.environ.get(_GUEST_IMAGE_ENV)
    if not image or not Path(image).exists():
        pytest.skip(
            f"{_GUEST_IMAGE_ENV} unset or points at a missing file; build the local-libvirt "
            f"rootfs with `python -m kdive build-fs` and set {_GUEST_IMAGE_ENV} to its "
            "--dest path (see docs/operating/runbooks/image-lifecycle.md)"
        )
    tree = os.environ.get(_KERNEL_TREE_ENV)
    if not tree or not Path(tree).exists():
        pytest.skip(
            f"{_KERNEL_TREE_ENV} unset or missing; run the fetch-kernel-tree fixture script"
        )
    db_url = os.environ.get(_DATABASE_URL_ENV)
    if not db_url:
        pytest.skip(f"{_DATABASE_URL_ENV} unset; bring up the stack (see the live-stack runbook)")
    issuer = require_issuer()  # skips if the OIDC issuer is unset/unreachable
    base_url = require_stack()  # skips if KDIVE_STACK_BASE_URL is unset
    return issuer, base_url, db_url


def _wire_preflight() -> tuple[OidcIssuer, str]:
    """Resolve issuer + stack URL for the RBAC-negative wire checks (no VM, no DB).

    These tests exercise a denial that fires in the auth/RBAC layer before any provisioning or DB
    read, so they need only a reachable issuer and a running server — not the guest image / kernel
    tree the booting spine requires. Gating them behind ``_spine_preflight`` would make them skip
    forever on any host without the (currently unbuildable) VM fixtures.
    """
    issuer = require_issuer()  # skips if the OIDC issuer is unset/unreachable
    base_url = require_stack()  # skips if KDIVE_STACK_BASE_URL is unset
    return issuer, base_url


# --- per-role tokens + profiles -------------------------------------------------------------


def _token(issuer: OidcIssuer, *, role: str, platform_roles: list[str] | None = None) -> str:
    return mint_role_token(
        issuer,
        project=_PROJECT,
        agent_session=_AGENT_SESSION,
        role=role,
        platform_roles=platform_roles,
    )


def _provision_profile() -> dict[str, object]:
    """A provisioning profile that opts force_crash in (the gate's profile factor, ADR-0045)."""
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": LOCAL_ALLOCATION_DISK_GB,
        "boot_method": "direct-kernel",
        "kernel_source_ref": os.environ[_KERNEL_TREE_ENV],
        "provider": {
            "local-libvirt": {
                "rootfs": {"kind": "local", "path": os.environ[_GUEST_IMAGE_ENV]},
                "crashkernel": "256M",
                "destructive_ops": ["force_crash"],
            }
        },
    }


def _build_profile() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kernel_source_ref": os.environ[_KERNEL_TREE_ENV],
        "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
    }


def _live_script_provision_profile() -> dict[str, object]:
    """Provision profile for the online drgn-live path: no force_crash.

    drgn-live needs no credential provisioning (ADR-0315): the loopback SSH forward renders on
    every domain (ADR-0281) and the transport authenticates with the per-System bootstrap key
    (ADR-0289). The introspect.script proof never crashes the guest, so this profile omits the
    `force_crash` destructive opt-in the crash spine needs.
    """
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": LOCAL_ALLOCATION_DISK_GB,
        "boot_method": "direct-kernel",
        "kernel_source_ref": os.environ[_KERNEL_TREE_ENV],
        "provider": {
            "local-libvirt": {
                "rootfs": {"kind": "local", "path": os.environ[_GUEST_IMAGE_ENV]},
                "crashkernel": "256M",
            }
        },
    }


# --- non-gated unit tests (CI-runnable; pin the equality invariant, ADR-0205) ----------------


def test_provision_profile_disk_gb_equals_allocation_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spine's provision profile disk_gb equals the allocate request's (ADR-0205, #656).

    ``reconcile_profile_sizing`` rejects a profile whose ``disk_gb`` differs from the
    allocation's resolved size, so the spine's ``_provision_profile()`` and the
    ``allocations.request`` it provisions against must declare the *same* disk. Both read one
    constant; this pins them so a future edit to either site can't silently re-introduce the
    self-conflict #656 named (provision profile ``disk_gb=20`` vs request ``disk_gb=10``).

    The passing case: reconciling the real spine profile against a snapshot built from the same
    constant succeeds and yields that disk. The conflicting case: a profile that over-asks is
    rejected as ``CONFIGURATION_ERROR``. The factory reads the kernel-tree / guest-image paths
    from the environment (real values gate the live spine); stub them so this stays CI-runnable.
    """
    monkeypatch.setenv(_KERNEL_TREE_ENV, "/nonexistent/kernel-src")
    monkeypatch.setenv(_GUEST_IMAGE_ENV, "/nonexistent/guest-image.qcow2")
    profile = _provision_profile()
    assert profile["disk_gb"] == LOCAL_ALLOCATION_DISK_GB

    snapshot = AllocationSizing(vcpu=2, memory_mb=2048, disk_gb=LOCAL_ALLOCATION_DISK_GB)
    reconciled = reconcile_profile_sizing(profile, snapshot)
    assert reconciled["disk_gb"] == LOCAL_ALLOCATION_DISK_GB

    over_asking = dict(profile, disk_gb=LOCAL_ALLOCATION_DISK_GB + 1)
    with pytest.raises(CategorizedError) as caught:
        reconcile_profile_sizing(over_asking, snapshot)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert caught.value.details["field"] == "disk_gb"


# --- RBAC negative: the raised path (no real system needed) ----------------------------------


@pytest.mark.live_stack
def test_viewer_denied_operator_op_over_the_wire() -> None:
    """A viewer token is denied an operator op; require_role raises → a tool error over HTTP.

    The viewer token carries the spine project (role ``viewer``), so the denial exercises the
    ``require_role`` (role) boundary, not the ``require_project`` (membership) boundary that
    ``allocations.request`` checks first.
    """
    issuer, base_url = _wire_preflight()

    async def _run() -> None:
        viewer = LiveStackClient.over_http(base_url, _token(issuer, role="viewer"))
        async with viewer:
            with pytest.raises(LiveStackToolError):  # require_role raises → tool error
                await viewer.call_tool(
                    "allocations.request",
                    project=_PROJECT,
                    request={"vcpus": 1, "memory_gb": 1, "resource": {"mode": "kind"}},
                )

    asyncio.run(_run())


@pytest.mark.live_stack
def test_report_all_projects_denied_to_project_token() -> None:
    """A project-only token is denied accounting.report_all_projects over the wire.

    Verified against the tool: the all-projects form catches the raised AuthorizationError and
    *returns* ToolResponse.failure(..., AUTHORIZATION_DENIED) — a well-formed error envelope, not a
    raised tool error. So assert the envelope shape (like crash-rbac-negative), not a raised
    LiveStackToolError (ADR-0046 §3).
    """
    issuer, base_url = _wire_preflight()

    async def _run() -> None:
        project_only = LiveStackClient.over_http(base_url, _token(issuer, role="viewer"))
        async with project_only:
            denied = await scalar(project_only, "accounting.report_all_projects")
        assert denied.status == "error", "project-only token was not denied (#101)"
        assert denied.error_category == "authorization_denied", "wrong denial category (#101)"

    asyncio.run(_run())


# --- the full spine -------------------------------------------------------------------------


@pytest.mark.live_stack
def test_spine_over_the_wire() -> None:
    """Drive allocate → … → teardown over HTTP; assert #1/#2/#3/#5; name the failing phase."""
    issuer, base_url, db_url = _spine_preflight()
    operator_token = _token(issuer, role="operator")
    admin_token = _token(issuer, role="admin")
    auditor_token = _token(issuer, role="viewer", platform_roles=["platform_auditor"])

    async def _run() -> None:
        from tests.integration.live_stack.spine import db_now  # noqa: PLC0415

        op = LiveStackClient.over_http(base_url, operator_token)
        admin = LiveStackClient.over_http(base_url, admin_token)
        system_id = allocation_id = run_id = ""
        async with op, admin:
            # out-of-band: meter the project (admission is fail-closed, ADR-0046 §0), then capture
            # the report window start from the DB clock (shares ledger.ts's clock).
            await seed_metering(db_url, _PROJECT)
            window_start = await db_now(db_url)
            async with phase("allocate"):
                env = ok(
                    await scalar(
                        op,
                        "allocations.request",
                        project=_PROJECT,
                        request={
                            "vcpus": 2,
                            "memory_gb": 2,
                            "disk_gb": LOCAL_ALLOCATION_DISK_GB,
                            "resource": {"mode": "kind"},
                        },
                    ),
                    "allocate",
                )
                allocation_id = env.object_id
            async with phase("provision"):
                env = ok(
                    await scalar(
                        op,
                        "systems.provision",
                        allocation_id=allocation_id,
                        profile=_provision_profile(),
                    ),
                    "provision",
                )
                system_id = data_str(env, "system_id")  # in data, NOT object_id (the job id)
                await await_system_state(op, "provision", system_id, "ready")
            async with phase("open-investigation"):
                env = ok(
                    await scalar(op, "investigations.open", project=_PROJECT, title="spine"),
                    "open-investigation",
                )
                investigation_id = env.object_id
            async with phase("create-run"):
                env = ok(
                    await scalar(
                        op,
                        "runs.create",
                        investigation_id=investigation_id,
                        system_id=system_id,
                        build_profile=_build_profile(),
                    ),
                    "create-run",
                )
                run_id = env.object_id
            for step in ("build", "install", "boot"):
                async with phase(step):
                    env = ok(await scalar(op, f"runs.{step}", run_id=run_id), step)
                    await drain_job(op, step, env.object_id)
            async with phase("attach"):
                env = ok(
                    await scalar(op, "debug.start_session", run_id=run_id, transport="gdbstub"),
                    "attach",
                )
                session_id = env.object_id
                ok(
                    await scalar(
                        op, "debug.read_registers", session_id=session_id, registers=["rip"]
                    ),
                    "attach",
                )
            async with phase("crash-rbac-negative"):
                denied = await scalar(op, "control.force_crash", system_id=system_id)
                if denied.status != "error" or denied.error_category != "authorization_denied":
                    raise SpinePhaseError("crash-rbac-negative", "operator was not denied")
            async with phase("crash"):
                ok(await scalar(admin, "control.force_crash", system_id=system_id), "crash")
                await await_system_state(admin, "crash", system_id, "crashed")
            async with phase("capture"):
                env = ok(await scalar(op, "vmcore.fetch", run_id=run_id), "capture")
                await drain_job(op, "capture", env.object_id)
                listing = ok(await scalar(op, "vmcore.list", run_id=run_id), "capture")
                refs = [v for item in listing.items for v in item.refs.values()]
                assert refs, "no vmcore artifact listed (#1)"
                # A raw core is `.../vmcore-{method}` (no `-redacted`); it must never surface.
                assert all(not ("/vmcore-" in r and not r.endswith("-redacted")) for r in refs), (
                    "raw vmcore leaked (#1)"
                )
            async with phase("introspect"):
                env = ok(await scalar(op, "introspect.from_vmcore", run_id=run_id), "introspect")
                report = json.dumps(data_mapping(env, "report"), sort_keys=True)
                assert "hunter2" not in report and "password=" not in report, "secret leaked (#3)"
            async with phase("release"):
                ok(
                    await scalar(op, "allocations.release", allocation_id=allocation_id),
                    "release",
                )
            async with phase("teardown"):  # reconciler-driven (≥30s) → torn_down
                await await_system_state(op, "teardown", system_id, "torn_down")
            async with phase("report"):  # all-projects rollup under platform_auditor
                await assert_report(
                    base_url,
                    auditor_token,
                    db_url,
                    window_start,
                    project=_PROJECT,
                    artifact_name=_ARTIFACT_NAME,
                )

        await assert_audit(
            db_url, project=_PROJECT, allocation_id=allocation_id, system_id=system_id
        )
        await _assert_teardown(db_url, system_id)

    asyncio.run(_run())


@pytest.mark.live_stack
def test_install_cmdline_sweep_two_boots_one_build_over_the_wire() -> None:
    """#988 acceptance: sweep two boot cmdlines against one built kernel, no rebuild (ADR-0299).

    allocate → provision → build (once) → install(dhash_entries=1) → boot →
    install(dhash_entries=2) → boot. Asserts each install's ``runs.get`` ``installed_cmdline``
    reflects the swept value and that the ``build`` step is only ever driven once (install
    re-stages, boot re-runs — no rebuild). Self-cleans (release).
    """
    issuer, base_url, _ = _spine_preflight()
    operator_token = _token(issuer, role="operator")

    async def _run() -> None:
        op = LiveStackClient.over_http(base_url, operator_token)
        allocation_id = ""
        async with op:
            async with phase("allocate"):
                env = ok(
                    await scalar(
                        op,
                        "allocations.request",
                        project=_PROJECT,
                        request={
                            "vcpus": 2,
                            "memory_gb": 2,
                            "disk_gb": LOCAL_ALLOCATION_DISK_GB,
                            "resource": {"mode": "kind"},
                        },
                    ),
                    "allocate",
                )
                allocation_id = env.object_id
            async with phase("provision"):
                env = ok(
                    await scalar(
                        op,
                        "systems.provision",
                        allocation_id=allocation_id,
                        profile=_provision_profile(),
                    ),
                    "provision",
                )
                system_id = data_str(env, "system_id")
                await await_system_state(op, "provision", system_id, "ready")
            async with phase("create-run"):
                env = ok(
                    await scalar(op, "investigations.open", project=_PROJECT, title="sweep"),
                    "create-run",
                )
                investigation_id = env.object_id
                env = ok(
                    await scalar(
                        op,
                        "runs.create",
                        investigation_id=investigation_id,
                        system_id=system_id,
                        build_profile=_build_profile(),
                    ),
                    "create-run",
                )
                run_id = env.object_id
            async with phase("build"):
                env = ok(await scalar(op, "runs.build", run_id=run_id), "build")
                await drain_job(op, "build", env.object_id)

            for variant in ("dhash_entries=1", "dhash_entries=2"):
                async with phase(f"install:{variant}"):
                    env = ok(
                        await scalar(op, "runs.install", run_id=run_id, cmdline=variant), variant
                    )
                    await drain_job(op, "install", env.object_id)
                async with phase(f"boot:{variant}"):
                    env = ok(await scalar(op, "runs.boot", run_id=run_id), variant)
                    await drain_job(op, "boot", env.object_id)
                got = ok(await scalar(op, "runs.get", run_id=run_id), "read-back")
                assert data_str(got, "installed_cmdline") == variant, (
                    f"runs.get installed_cmdline must reflect the swept variant {variant}"
                )
                # The build step never re-runs across the sweep (no rebuild).
                steps = data_mapping(got, "steps")
                assert steps["build"] == "succeeded"

            async with phase("release"):
                ok(await scalar(op, "allocations.release", allocation_id=allocation_id), "release")

    asyncio.run(_run())


@pytest.mark.live_stack
def test_spine_live_script_over_the_wire() -> None:
    """Boot a guest, attach drgn-live, and run a real caller drgn script via introspect.script.

    The online half of the introspect surface (#762, ADR-0240): allocate → … → boot →
    debug.start_session("drgn-live") → introspect.script(real `drgn -k` script). Asserts the script
    ran in the live guest kernel (a proof marker + a non-empty live task walk) and that an
    over-cap script is rejected before any guest send. Self-cleans (end_session + release).
    """
    issuer, base_url, db_url = _spine_preflight()
    operator_token = _token(issuer, role="operator")

    async def _run() -> None:
        op = LiveStackClient.over_http(base_url, operator_token)
        allocation_id = session_id = ""
        async with op:
            await seed_metering(db_url, _PROJECT)
            async with phase("allocate"):
                env = ok(
                    await scalar(
                        op,
                        "allocations.request",
                        project=_PROJECT,
                        request={
                            "vcpus": 2,
                            "memory_gb": 2,
                            "disk_gb": LOCAL_ALLOCATION_DISK_GB,
                            "resource": {"mode": "kind"},
                        },
                    ),
                    "allocate",
                )
                allocation_id = env.object_id
            async with phase("provision"):
                env = ok(
                    await scalar(
                        op,
                        "systems.provision",
                        allocation_id=allocation_id,
                        profile=_live_script_provision_profile(),
                    ),
                    "provision",
                )
                system_id = data_str(env, "system_id")
                await await_system_state(op, "provision", system_id, "ready")
            async with phase("open-investigation"):
                env = ok(
                    await scalar(op, "investigations.open", project=_PROJECT, title="live-script"),
                    "open-investigation",
                )
                investigation_id = env.object_id
            async with phase("create-run"):
                env = ok(
                    await scalar(
                        op,
                        "runs.create",
                        investigation_id=investigation_id,
                        system_id=system_id,
                        build_profile=_build_profile(),
                    ),
                    "create-run",
                )
                run_id = env.object_id
            for step in ("build", "install", "boot"):
                async with phase(step):
                    env = ok(await scalar(op, f"runs.{step}", run_id=run_id), step)
                    await drain_job(op, step, env.object_id)
            async with phase("attach-drgn-live"):
                env = ok(
                    await scalar(op, "debug.start_session", run_id=run_id, transport="drgn-live"),
                    "attach-drgn-live",
                )
                session_id = env.object_id
            async with phase("introspect-script"):
                env = ok(
                    await scalar(
                        op,
                        "introspect.script",
                        session_id=session_id,
                        script=_PROOF_SCRIPT,
                        timeout_sec=30.0,
                    ),
                    "introspect-script",
                )
                output = data_str(env, "output")
                assert "DRGN_LIVE_PROOF" in output, f"proof marker missing: {output!r}"
                assert "ntasks=" in output and "ntasks=0" not in output, (
                    f"no live task walk: {output!r}"
                )
                assert env.data["truncated"] is False, "unexpected truncation under cap"
            async with phase("oversize-script-rejected"):
                denied = await scalar(
                    op,
                    "introspect.script",
                    session_id=session_id,
                    script=_OVERSIZE_SCRIPT,
                    timeout_sec=30.0,
                )
                if denied.status != "error" or denied.error_category != "configuration_error":
                    raise SpinePhaseError(
                        "oversize-script-rejected",
                        "over-cap script not rejected",
                        error_category=denied.error_category,
                    )
            async with phase("end-session"):
                ok(await scalar(op, "debug.end_session", session_id=session_id), "end-session")
            async with phase("release"):
                ok(
                    await scalar(op, "allocations.release", allocation_id=allocation_id),
                    "release",
                )

    asyncio.run(_run())


# --- per-family SSH-reachability proof (#956, ADR-0294) -------------------------------------


def _reachability_preflight(family: str) -> tuple[OidcIssuer, str, str, str]:
    """Resolve issuer + stack + db + the per-family ready image, or skip with the exact fix.

    Reuses the spine's stack/issuer/db + kernel-tree skip idiom (ADR-0035 §4) and adds the
    per-family image env var, so a host that lacks this family's image (or the kernel tree) skips
    *this parameter* cleanly rather than erroring at provision-time.
    """
    image_env = _FAMILY_IMAGE_ENV[family]
    image = os.environ.get(image_env)
    if not image or not Path(image).exists():
        pytest.skip(
            f"{image_env} unset or points at a missing file; build a {family}-family "
            f"*-kdive-ready rootfs with `python -m kdive build-fs` and set {image_env} to its "
            "--dest path (see docs/operating/runbooks/image-lifecycle.md)"
        )
    tree = os.environ.get(_KERNEL_TREE_ENV)
    if not tree or not Path(tree).exists():
        pytest.skip(
            f"{_KERNEL_TREE_ENV} unset or missing; run the fetch-kernel-tree fixture script"
        )
    db_url = os.environ.get(_DATABASE_URL_ENV)
    if not db_url:
        pytest.skip(f"{_DATABASE_URL_ENV} unset; bring up the stack (see the live-stack runbook)")
    issuer = require_issuer()  # skips if the OIDC issuer is unset/unreachable
    base_url = require_stack()  # skips if KDIVE_STACK_BASE_URL is unset
    return issuer, base_url, db_url, image


def _reachability_provision_profile(image: str) -> dict[str, object]:
    """A minimal direct-kernel profile for the reachability proof.

    The loopback SSH forward + virtio NIC render on *every* local-libvirt provision (ADR-0281), so
    the profile carries no credential field — drgn-live authenticates with the per-System bootstrap
    key (ADR-0289/0315). No ``force_crash`` (no destructive op needed).
    """
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": LOCAL_ALLOCATION_DISK_GB,
        "boot_method": "direct-kernel",
        "kernel_source_ref": os.environ[_KERNEL_TREE_ENV],
        "provider": {
            "local-libvirt": {
                "rootfs": {"kind": "local", "path": image},
                "crashkernel": "256M",
            }
        },
    }


@pytest.mark.live_stack
@pytest.mark.parametrize("family", ["debian", "rhel"])
def test_family_guest_is_ssh_reachable_over_the_wire(family: str) -> None:
    """Prove the always-rendered loopback SSH forward reaches a per-family guest sshd (#956).

    allocate → provision (no build/install/boot; the baseline kernel boots to ``ready`` and the
    forward renders at provision) → ``systems.ssh_info`` returns a ``worker_loopback`` endpoint →
    ``systems.authorize_ssh_key`` drains to a *succeeded* job. The drained success is the
    load-bearing proof: the worker SSHes into the guest over the per-System managed key, which only
    succeeds if the NIC leased, the forward bridged, and sshd answered — the exact contract #956
    said was proven per family only by assumption. Self-cleans (release) on exit.
    """
    issuer, base_url, db_url, image = _reachability_preflight(family)
    operator_token = _token(issuer, role="operator")

    async def _run() -> None:
        op = LiveStackClient.over_http(base_url, operator_token)
        allocation_id = ""
        async with op:
            await seed_metering(db_url, _PROJECT)
            try:
                async with phase(f"{family}:allocate"):
                    env = ok(
                        await scalar(
                            op,
                            "allocations.request",
                            project=_PROJECT,
                            request={
                                "vcpus": 2,
                                "memory_gb": 2,
                                "disk_gb": LOCAL_ALLOCATION_DISK_GB,
                                "resource": {"mode": "kind"},
                            },
                        ),
                        f"{family}:allocate",
                    )
                    allocation_id = env.object_id
                async with phase(f"{family}:provision"):
                    env = ok(
                        await scalar(
                            op,
                            "systems.provision",
                            allocation_id=allocation_id,
                            profile=_reachability_provision_profile(image),
                        ),
                        f"{family}:provision",
                    )
                    system_id = data_str(env, "system_id")
                    await await_system_state(op, f"{family}:provision", system_id, "ready")
                async with phase(f"{family}:ssh_info"):
                    info = ok(
                        await scalar(op, "systems.ssh_info", system_id=system_id),
                        f"{family}:ssh_info",
                    )
                    ssh = data_mapping(info, "ssh")
                    assert ssh["host_scope"] == "worker_loopback", f"unexpected ssh_info: {ssh!r}"
                    assert ssh["host"] and isinstance(ssh["port"], int), (
                        f"ssh_info returned no endpoint on a ready System: {ssh!r}"
                    )
                async with phase(f"{family}:authorize_ssh_key"):
                    env = ok(
                        await scalar(
                            op,
                            "systems.authorize_ssh_key",
                            system_id=system_id,
                            public_key=_REACHABILITY_PUBKEY,
                        ),
                        f"{family}:authorize_ssh_key",
                    )
                    # A *succeeded* drain is the reachability proof: a non-succeeded drain raises a
                    # SpinePhaseError naming this family + the job's error_category.
                    await drain_job(op, f"{family}:authorize_ssh_key", env.object_id)
            finally:
                if allocation_id:
                    await scalar(op, "allocations.release", allocation_id=allocation_id)

    asyncio.run(_run())


async def _assert_teardown(db_url: str, system_id: str) -> None:
    """#5: after teardown the System is torn_down and no local-libvirt OwnedInfra remains."""
    assert await system_torn_down(db_url, system_id), "system not torn_down (#5)"
    import libvirt  # noqa: PLC0415 — only importable on a libvirt host (the live_stack path)

    from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery  # noqa: PLC0415

    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: libvirt.open("qemu:///system"),  # ty: ignore[invalid-argument-type]
        concurrent_allocation_cap=2,
    )
    owned_ids = {o["system_id"] for o in disc.list_owned()}
    assert system_id not in owned_ids, "released system still owned (#5)"
