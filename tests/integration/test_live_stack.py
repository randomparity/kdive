"""The phase-structured local-libvirt live-stack spine driver (#100, ADR-0042 §1/§4/§5, ADR-0045).

Drives the full kdive spine — allocate → provision → open-investigation → create-run → upload-build
→ install → boot → attach → crash → capture → introspect → release → (reconciler) teardown → report
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
import base64
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.sizing import AllocationSizing
from kdive.mcp.dev_harness import (
    LiveStackClient,
    LiveStackToolError,
    OidcIssuer,
)
from kdive.mcp.responses import ToolResponse
from kdive.profiles.provisioning import reconcile_profile_sizing
from tests.integration.live_stack.conftest import (
    expected_accel,
    require_guest_arch,
    require_issuer,
    require_stack,
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
# The ppc64le rootfs for the live TCG boot proof (#1144, epic #1139): a Fedora ppc64le image
# published under rootfs/local/. Distinct from the x86_64 family images — it boots under TCG
# emulation on the x86_64 host, so the preflight also gates on that emulator via require_guest_arch.
_PPC64LE_IMAGE_ENV = "KDIVE_GUEST_IMAGE_PPC64LE"
# A Fedora ppc64le guest under TCG boots far slower than a native KVM guest — the first boot
# runs the SELinux relabel and reboots, then the second boot brings up cloud-init networking and
# sshd (~several minutes wall-clock on the x86_64 host). Poll the reachability probe under a
# generous deadline rather than the fast KVM-tuned authorize_ssh_key pre-flight (#1144).
_PPC64LE_REACHABLE_DEADLINE_S = 900.0
_PPC64LE_REACHABLE_POLL_S = 20.0
# The #1146 boot proof also uploads a ppc64le kernel *bundle* and boots it via the install plane.
# KDIVE_PPC64LE_BUNDLE points at a directory holding `kernel.tar.gz` (the ADR-0343 combined tar:
# an ELF `boot/vmlinuz` + `lib/modules/<ver>/`) and `initrd.img`. Build it from the published
# ppc64le rootfs per docs/design/2026-07-13-ppc64le-boot-bundle-proof-record-1146.md; the test
# skips cleanly when it is absent.
_PPC64LE_BUNDLE_ENV = "KDIVE_PPC64LE_BUNDLE"
# The boot window under TCG (upload+install+boot of an uploaded modular kernel) is generous.
_PPC64LE_BOOT_DEADLINE_S = 1800.0
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
    """The Run build profile for the x86_64 spine (upload-only lane, ADR-0337).

    The server-build lane was removed, so ``BuildProfile`` accepts only ``schema_version`` + the
    target ``arch`` (``extra="forbid"``); the kernel bytes now arrive via the external-upload lane
    (see ``_build_and_upload_kernel``), not a server ``kernel_source_ref``/``config`` build.
    """
    return {"schema_version": 1, "arch": "x86_64"}


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


# --- external-build upload lane (shared; ADR-0234/0337) -------------------------------------


def _sha256_b64(path: Path) -> str:
    """The base64 SHA-256 the upload contract declares (S3 ``x-amz-checksum-sha256``)."""
    return base64.b64encode(hashlib.sha256(path.read_bytes()).digest()).decode()


async def _put_presigned(item: ToolResponse, path: Path) -> None:
    """PUT ``path`` to a ``create_run_upload`` item's presigned URL + required headers."""
    url = item.refs["upload_url"]
    raw_headers = item.data.get("required_headers", {})
    headers = {k: str(v) for k, v in raw_headers.items()} if isinstance(raw_headers, dict) else {}
    async with httpx.AsyncClient(timeout=180.0) as http:
        resp = await http.put(url, content=path.read_bytes(), headers=headers)
        resp.raise_for_status()


def _combined_kernel_tar(kernel_src: Path, dest_dir: Path) -> Path:
    """Cut the ADR-0234 combined ``kernel`` artifact from a built x86_64 kernel tree.

    Reproduces the ``external-build-upload`` recipe (mirrors ``scripts/live-debug.py`` after #1219):
    stage the module tree with ``make modules_install`` into ``dest_dir`` and tar
    ``arch/x86/boot/bzImage`` (renamed to ``boot/vmlinuz``, listed first so ``lib/modules`` lands
    inside the validator's decompress-scan bound) plus ``lib/modules`` into one gzip tar, dropping
    the ``build``/``source`` back-symlinks.

    Args:
        kernel_src: A *built* x86_64 kernel tree (must contain ``arch/x86/boot/bzImage``).
        dest_dir: A scratch directory to stage modules and write ``kernel.tar.gz`` into.

    Returns:
        The path to the combined ``kernel.tar.gz`` under ``dest_dir``.

    Raises:
        RuntimeError: If ``kernel_src`` holds no built bzImage.
    """
    bzimage = kernel_src / "arch/x86/boot/bzImage"
    if not bzimage.is_file():
        raise RuntimeError(
            f"no built bzImage at {bzimage}; build the kernel tree at {kernel_src} first"
        )
    modstage = dest_dir / "modstage"
    subprocess.run(
        ["make", "-C", str(kernel_src), "modules_install", f"INSTALL_MOD_PATH={modstage}"],
        check=True,
    )
    tar_path = dest_dir / "kernel.tar.gz"
    subprocess.run(
        [
            "tar",
            "-czf",
            str(tar_path),
            "--exclude=*/build",
            "--exclude=*/source",
            "--transform=s|^arch/x86/boot/bzImage$|boot/vmlinuz|",
            "-C",
            str(kernel_src),
            "arch/x86/boot/bzImage",
            "-C",
            str(modstage),
            "lib/modules",
        ],
        check=True,
    )
    return tar_path


def _accepted_run_upload_names(contract: ToolResponse) -> set[str]:
    """The ``run`` owner-kind's accepted names from an ``artifacts.expected_uploads`` contract."""
    for item in contract.items:
        data = item.data or {}
        if data.get("owner_kind") == "run":
            names = data.get("accepted_names", [])
            return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()
    return set()


async def _build_and_upload_kernel(op: LiveStackClient, *, run_id: str) -> None:
    """Build the x86_64 kernel tar from ``KDIVE_KERNEL_SRC`` and drive the external-upload lane.

    Replaces the removed server-build lane (``runs.build``): discover the contract
    (``artifacts.expected_uploads``), cut the combined ``kernel`` tar, declare + PUT it via
    ``artifacts.create_run_upload``, then ``runs.complete_build``. The Run goes CREATED → SUCCEEDED
    with ``steps.build == succeeded`` (set by construction in steps.py), ready for ``runs.install``.
    """
    contract = ok(await scalar(op, "artifacts.expected_uploads"), "upload-build")
    accepted = _accepted_run_upload_names(contract)
    if "kernel" not in accepted:
        raise SpinePhaseError(
            "upload-build", f"upload contract no longer accepts 'kernel': {accepted}"
        )
    with tempfile.TemporaryDirectory(prefix="kdive-spine-kernel-") as scratch:
        kernel_tar = _combined_kernel_tar(Path(os.environ[_KERNEL_TREE_ENV]), Path(scratch))
        decls = [
            {
                "name": "kernel",
                "sha256": _sha256_b64(kernel_tar),
                "size_bytes": kernel_tar.stat().st_size,
            }
        ]
        up = ok(
            await scalar(op, "artifacts.create_run_upload", run_id=run_id, artifacts=decls),
            "upload-build",
        )
        by_name = {item.data.get("name"): item for item in up.items}
        if "kernel" not in by_name:
            raise SpinePhaseError("upload-build", "create_run_upload returned no 'kernel' item")
        await _put_presigned(by_name["kernel"], kernel_tar)
    ok(await scalar(op, "runs.complete_build", run_id=run_id), "upload-build")


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


def test_ppc64le_provision_profile_disk_gb_equals_allocation_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ppc64le reachability profile obeys the same disk-equality invariant, at arch=ppc64le.

    The live proof (#1144) is ``live_stack``-gated, so this non-gated companion keeps a CI
    assertion on the new ppc64le factory: its ``disk_gb`` equals the allocation constant (ADR-0205)
    and its ``arch`` is ``ppc64le`` (the field that routes provisioning through the pseries traits).
    """
    monkeypatch.setenv(_KERNEL_TREE_ENV, "/nonexistent/kernel-src")
    profile = _reachability_provision_profile(
        "/nonexistent/fedora-ppc64le.qcow2", arch="ppc64le", crashkernel="512M"
    )
    assert profile["arch"] == "ppc64le"
    assert profile["disk_gb"] == LOCAL_ALLOCATION_DISK_GB

    snapshot = AllocationSizing(vcpu=2, memory_mb=2048, disk_gb=LOCAL_ALLOCATION_DISK_GB)
    reconciled = reconcile_profile_sizing(profile, snapshot)
    assert reconciled["disk_gb"] == LOCAL_ALLOCATION_DISK_GB


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
                    **{"vcpus": 1, "memory_gb": 1, "resource": {"mode": "kind"}},
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
                        **{
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
                    await scalar(
                        op,
                        "investigations.open",
                        **{"project": _PROJECT, "title": "spine"},
                    ),
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
            async with phase("upload-build"):
                await _build_and_upload_kernel(op, run_id=run_id)
            for step in ("install", "boot"):
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
    """#988 acceptance: sweep two boot cmdlines, one uploaded kernel, no re-upload (ADR-0299).

    allocate → provision → upload-build (once) → install(dhash_entries=1) → boot →
    install(dhash_entries=2) → boot. Asserts each install's ``runs.get`` ``installed_cmdline``
    reflects the swept value and that the ``build`` step stays ``succeeded`` across the sweep
    (install re-stages, boot re-runs — no re-upload). Self-cleans (release).
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
                        **{
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
                    await scalar(
                        op,
                        "investigations.open",
                        **{"project": _PROJECT, "title": "sweep"},
                    ),
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
            async with phase("upload-build"):
                await _build_and_upload_kernel(op, run_id=run_id)

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
                # The build step stays succeeded across the sweep (no re-upload).
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
                        **{
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
                    await scalar(
                        op,
                        "investigations.open",
                        **{"project": _PROJECT, "title": "live-script"},
                    ),
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
            async with phase("upload-build"):
                await _build_and_upload_kernel(op, run_id=run_id)
            for step in ("install", "boot"):
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


def _reachability_provision_profile(
    image: str, *, arch: str = "x86_64", crashkernel: str = "256M"
) -> dict[str, object]:
    """A minimal direct-kernel profile for the reachability proof.

    The loopback SSH forward + virtio NIC render on *every* local-libvirt provision (ADR-0281), so
    the profile carries no credential field — drgn-live authenticates with the per-System bootstrap
    key (ADR-0289/0315). No ``force_crash`` (no destructive op needed).

    ``arch`` selects the guest arch (``ppc64le`` for the #1144 TCG boot proof; admission then
    persists ``accel=tcg`` and the provisioner renders a pseries/qemu domain). ``kernel_source_ref``
    is a required-but-unread ``direct-kernel`` token — provision boots the rootfs's *own* baseline
    kernel (ADR-0272), so the x86_64 kernel tree is a valid arch-opaque value for a ppc64le guest.
    """
    return {
        "schema_version": 1,
        "arch": arch,
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": LOCAL_ALLOCATION_DISK_GB,
        "boot_method": "direct-kernel",
        "kernel_source_ref": os.environ[_KERNEL_TREE_ENV],
        "provider": {
            "local-libvirt": {
                "rootfs": {"kind": "local", "path": image},
                "crashkernel": crashkernel,
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
                            **{
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


# --- ppc64le live TCG boot proof (#1144, epic #1139) ---------------------------------------


def _ppc64le_reachability_preflight() -> tuple[OidcIssuer, str, str, str]:
    """Resolve issuer + stack + db + the ppc64le image, or skip with the exact fix.

    Adds the ppc64le emulator gate to the reachability preflight idiom: a host without
    ``qemu-system-ppc64`` cannot boot a pseries guest under TCG, so it skips this proof cleanly
    rather than erroring at define-time.
    """
    require_guest_arch("ppc64le")
    image = os.environ.get(_PPC64LE_IMAGE_ENV)
    if not image or not Path(image).exists():
        pytest.skip(
            f"{_PPC64LE_IMAGE_ENV} unset or points at a missing file; publish the Fedora ppc64le "
            "rootfs (see docs/design/2026-07-13-ppc64le-fixture-live-proof-1144.md §4) and set "
            f"{_PPC64LE_IMAGE_ENV} to its path"
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


@pytest.mark.live_stack
@pytest.mark.live_vm_tcg
def test_ppc64le_guest_is_ssh_reachable_over_the_wire() -> None:
    """Prove a Fedora ppc64le guest boots end-to-end over the wire (#1144; native KVM-HV #1156).

    allocate → provision (``arch=ppc64le``: admission persists the host-resolved accel, ADR-0339 —
    ``accel=tcg`` on the x86_64 CI host, ``accel=kvm`` on a POWER host; the provisioner renders a
    pseries/qemu/emulator domain, ADR-0340; the boot handler applies the accel-scaled deadline,
    ADR-0341, and direct-kernel-boots the rootfs's own baseline ppc64le kernel under QEMU/SLOF,
    ADR-0272) → the System reaches ``ready`` (the domain is defined+started;
    the baseline boot is optimistic, ADR-0272) → poll ``systems.check_ssh_reachable`` until the
    guest answers an SSH banner. That reachable verdict is the load-bearing proof: it means the
    ppc64le kernel booted to userspace under TCG (no ISA/CPU fault), the virtio NIC leased its DHCP
    address without a pinned PCI slot (retiring PR #1070's ``pin_nic_slot=False``), and sshd is up.

    The ``kdive-ready`` marker on ``hvc0`` (spapr-vty, retiring PR #1070's ``console_device``) is
    proven separately in the committed boot-console record
    (``docs/design/2026-07-13-ppc64le-tcg-boot-proof-record-1144.md``): reaching ``ready`` here is
    optimistic (ADR-0272 provision does not await the console marker), so this test asserts SSH
    reachability, not the marker.

    Vehicle note: ``live_stack`` is the repo's only end-to-end provision→boot path; this is a
    live-VM-class proof under the host-resolved accelerator (TCG on x86_64, KVM-HV on a POWER host,
    #1156). The distinct ``live_vm``/``live_vm_tcg`` marker split is epic issue 15's scope, not
    here. Self-cleans (release) on exit.
    """
    issuer, base_url, db_url, image = _ppc64le_reachability_preflight()
    operator_token = _token(issuer, role="operator")

    async def _run() -> None:
        op = LiveStackClient.over_http(base_url, operator_token)
        allocation_id = ""
        async with op:
            await seed_metering(db_url, _PROJECT)
            try:
                async with phase("ppc64le:allocate"):
                    env = ok(
                        await scalar(
                            op,
                            "allocations.request",
                            project=_PROJECT,
                            **{
                                "vcpus": 2,
                                "memory_gb": 2,
                                "disk_gb": LOCAL_ALLOCATION_DISK_GB,
                                "resource": {"mode": "kind"},
                            },
                        ),
                        "ppc64le:allocate",
                    )
                    allocation_id = env.object_id
                async with phase("ppc64le:provision"):
                    env = ok(
                        await scalar(
                            op,
                            "systems.provision",
                            allocation_id=allocation_id,
                            profile=_reachability_provision_profile(
                                image, arch="ppc64le", crashkernel="512M"
                            ),
                        ),
                        "ppc64le:provision",
                    )
                    system_id = data_str(env, "system_id")
                    await await_system_state(op, "ppc64le:provision", system_id, "ready")
                async with phase("ppc64le:systems_get"):
                    got = ok(
                        await scalar(op, "systems.get", system_id=system_id),
                        "ppc64le:systems_get",
                    )
                    # The persisted accel is the recorded fact the deadline scaling keyed off. It
                    # is host-resolved (ADR-0339): ``tcg`` for a foreign-arch guest (ppc64le on the
                    # x86_64 CI host), ``kvm`` for a native guest (ppc64le on a POWER host, #1156).
                    want_accel = expected_accel("ppc64le")
                    assert data_str(got, "accel") == want_accel, (
                        f"expected accel={want_accel}: {got!r}"
                    )
                async with phase("ppc64le:ssh_info"):
                    info = ok(
                        await scalar(op, "systems.ssh_info", system_id=system_id),
                        "ppc64le:ssh_info",
                    )
                    ssh = data_mapping(info, "ssh")
                    assert ssh["host_scope"] == "worker_loopback", f"unexpected ssh_info: {ssh!r}"
                    assert ssh["host"] and isinstance(ssh["port"], int), (
                        f"ssh_info returned no endpoint on a ready System: {ssh!r}"
                    )
                async with phase("ppc64le:await_ssh_reachable"):
                    # A reachable verdict is the load-bearing proof: an SSH banner from the guest
                    # means the ppc64le kernel booted end-to-end under TCG, the virtio NIC leased
                    # its DHCP address without a pinned PCI slot (pin_nic_slot=False), and sshd is
                    # up. authorize_ssh_key's pre-flight fails a not-yet-booted guest terminally, so
                    # the slow TCG boot is spanned by polling the read-only reachability probe
                    # (which succeeds whether reachable or not, carrying the verdict inline) under a
                    # generous deadline.
                    deadline = time.monotonic() + _PPC64LE_REACHABLE_DEADLINE_S
                    while True:
                        probe = ok(
                            await scalar(op, "systems.check_ssh_reachable", system_id=system_id),
                            "ppc64le:await_ssh_reachable",
                        )
                        done = await drain_job(op, "ppc64le:await_ssh_reachable", probe.object_id)
                        verdict_json = done.refs.get("result")
                        assert verdict_json is not None, (
                            f"check_ssh_reachable succeeded with no result verdict: {done!r}"
                        )
                        verdict = json.loads(verdict_json)
                        if verdict.get("reachable"):
                            break
                        # On an unreachable verdict the probe attaches a redacted console tail
                        # (ADR-0306) — the boot-stall evidence a timeout most needs; surface it so a
                        # guest that never boots (e.g. a future ISA/CPU regression) self-diagnoses.
                        detail = verdict.get("detail")
                        tail = verdict.get("console_tail")
                        assert time.monotonic() < deadline, (
                            f"guest never became SSH-reachable under TCG ({detail!r}); "
                            f"console tail: {tail!r}"
                        )
                        await asyncio.sleep(_PPC64LE_REACHABLE_POLL_S)
            finally:
                if allocation_id:
                    await scalar(op, "allocations.release", allocation_id=allocation_id)

    asyncio.run(_run())


# --- #1146: uploaded ppc64le kernel bundle boots via the install plane (epic #1139) ----------


def _ppc64le_bundle_preflight() -> tuple[OidcIssuer, str, str, Path, Path]:
    """Reachability preflight + the uploaded-bundle artifacts, or skip with the exact fix."""
    issuer, base_url, _, image = _ppc64le_reachability_preflight()
    bundle = os.environ.get(_PPC64LE_BUNDLE_ENV)
    if not bundle:
        pytest.skip(
            f"{_PPC64LE_BUNDLE_ENV} unset; build kernel.tar.gz + initrd.img from the ppc64le "
            "rootfs (see the #1146 proof-record doc) and point it here"
        )
    kernel_tar, initrd = Path(bundle) / "kernel.tar.gz", Path(bundle) / "initrd.img"
    if not kernel_tar.exists() or not initrd.exists():
        pytest.skip(f"{_PPC64LE_BUNDLE_ENV} must contain kernel.tar.gz and initrd.img")
    return issuer, base_url, image, kernel_tar, initrd


@pytest.mark.live_stack
@pytest.mark.live_vm_tcg
def test_ppc64le_uploaded_kernel_bundle_boots_over_the_wire() -> None:
    """Prove an *uploaded* ppc64le bundle installs and direct-kernel-boots on pseries (#1146).

    Extends the #1144 baseline-boot proof through the **install plane**: a provisioned ppc64le
    System (``accel=tcg``) gets an uploaded combined kernel tar (ELF ``boot/vmlinuz`` +
    ``lib/modules/<ver>/``, validated as a ppc64le ELF at ``complete_build``, ADR-0343) plus an
    ``initrd``; ``runs.install`` extracts the ELF via ``extract_kernel_bundle`` and redefines the
    direct-kernel ``<os>``; ``runs.boot`` power-cycles into it under the TCG-scaled deadline
    (ADR-0341) and reaches readiness — proving the boot path (ADR-0344) is arch-opaque for the ELF
    payload, not bzImage-literal.

    The readiness verdict is **discriminating** (not confounded with #1144's baseline boot of the
    same bytes): the running domain's ``<kernel>``/``<initrd>`` must resolve to the *per-Run* staged
    path, and a unique ``kdive_proof_token`` passed at install must reach ``<cmdline>``. Reaching
    ``runs.boot`` readiness means ``kdive-ready`` fired on ``hvc0`` (real-root, post-pivot,
    ADR-0342/0055) — the positive initrd-addressing signal (no pseries ``<initrd>`` quirk).

    Skips cleanly without ``qemu-system-ppc64`` / the published rootfs / the uploaded bundle. The
    guest-kernel-writer's module injection is a separate path (host-side ``depmod`` as of #1148,
    ADR-0346); this plain boot injects no modules, so it does not exercise it (ADR-0344).
    Self-cleans.
    """
    issuer, base_url, image, kernel_tar, initrd = _ppc64le_bundle_preflight()
    operator_token = _token(issuer, role="operator")
    proof_token = f"kdive_proof_token={uuid4().hex[:12]}"

    async def _run() -> None:
        op = LiveStackClient.over_http(base_url, operator_token)
        allocation_id = ""
        async with op:
            try:
                async with phase("ppc64le-bundle:allocate"):
                    env = ok(
                        await scalar(
                            op,
                            "allocations.request",
                            project=_PROJECT,
                            **{
                                "vcpus": 2,
                                "memory_gb": 2,
                                "disk_gb": LOCAL_ALLOCATION_DISK_GB,
                                "resource": {"mode": "kind"},
                            },
                        ),
                        "ppc64le-bundle:allocate",
                    )
                    allocation_id = env.object_id
                async with phase("ppc64le-bundle:provision"):
                    # No crashkernel → CaptureMethod.CONSOLE → a PLAIN boot (no module injection),
                    # so this exercises extract_kernel_bundle + <os> render + SLOF boot only.
                    profile = _reachability_provision_profile(image, arch="ppc64le")
                    env = ok(
                        await scalar(
                            op, "systems.provision", allocation_id=allocation_id, profile=profile
                        ),
                        "ppc64le-bundle:provision",
                    )
                    system_id = data_str(env, "system_id")
                    await await_system_state(op, "ppc64le-bundle:provision", system_id, "ready")
                async with phase("ppc64le-bundle:create-run"):
                    env = ok(
                        await scalar(
                            op,
                            "investigations.open",
                            **{"project": _PROJECT, "title": "ppc64le-bundle-1146"},
                        ),
                        "ppc64le-bundle:create-run",
                    )
                    env = ok(
                        await scalar(
                            op,
                            "runs.create",
                            **{
                                "investigation_id": env.object_id,
                                "system_id": system_id,
                                "build_profile": {"schema_version": 1, "arch": "ppc64le"},
                            },
                        ),
                        "ppc64le-bundle:create-run",
                    )
                    run_id = env.object_id
                async with phase("ppc64le-bundle:upload"):
                    decls = [
                        {
                            "name": "kernel",
                            "sha256": _sha256_b64(kernel_tar),
                            "size_bytes": kernel_tar.stat().st_size,
                        },
                        {
                            "name": "initrd",
                            "sha256": _sha256_b64(initrd),
                            "size_bytes": initrd.stat().st_size,
                        },
                    ]
                    up = ok(
                        await scalar(
                            op, "artifacts.create_run_upload", run_id=run_id, artifacts=decls
                        ),
                        "ppc64le-bundle:upload",
                    )
                    by_name = {item.data.get("name"): item for item in up.items}
                    await _put_presigned(by_name["kernel"], kernel_tar)
                    await _put_presigned(by_name["initrd"], initrd)
                    ok(
                        await scalar(op, "runs.complete_build", run_id=run_id),
                        "ppc64le-bundle:upload",
                    )
                async with phase("ppc64le-bundle:install"):
                    env = ok(
                        await scalar(op, "runs.install", run_id=run_id, cmdline=proof_token),
                        "ppc64le-bundle:install",
                    )
                    await drain_job(op, "ppc64le-bundle:install", env.object_id)
                async with phase("ppc64le-bundle:boot"):
                    env = ok(await scalar(op, "runs.boot", run_id=run_id), "ppc64le-bundle:boot")
                    # Reaching readiness == kdive-ready on hvc0 (real-root, post-pivot): the
                    # uploaded ELF booted on pseries/TCG + the initramfs pivoted (no initrd quirk).
                    await drain_job(
                        op,
                        "ppc64le-bundle:boot",
                        env.object_id,
                        deadline_s=_PPC64LE_BOOT_DEADLINE_S,
                    )
                async with phase("ppc64le-bundle:attribute"):
                    # Discriminating: the running domain boots the *per-Run staged* uploaded bundle
                    # (not the provision-time baseline); the unique install token reached cmdline.
                    xml = subprocess.run(
                        ["virsh", "-c", "qemu:///system", "dumpxml", f"kdive-{system_id}"],
                        capture_output=True,
                        text=True,
                        check=True,
                    ).stdout
                    assert run_id in xml, "domain <kernel>/<initrd> not at the per-Run staged path"
                    assert proof_token in xml, "install cmdline token did not reach <cmdline>"
                    assert "pseries" in xml, "domain is not a pseries machine"
            finally:
                if allocation_id:
                    await scalar(op, "allocations.release", allocation_id=allocation_id)

    asyncio.run(_run())


def _ppc64le_kdump_provision_profile(image: str) -> dict[str, object]:
    """A ppc64le KDUMP + force_crash provision profile for the #1148 capture proof.

    ``crashkernel`` is set to a **sentinel** ``"256M"`` — different from the ppc64le arch default
    (512M, ADR-0346) — for the sole purpose of selecting ``CaptureMethod.KDUMP`` (the profile token
    is only a method signal; ``capture_method`` keys off its presence, never its value). No
    per-install ``crashkernel`` is passed at ``runs.install``, so the *arch default* 512M is what
    actually reaches the boot cmdline. Observing ``crashkernel=512M`` (not ``256M``) in the running
    domain's ``<cmdline>`` therefore proves the arch default sized the reservation. ``memory_mb`` is
    2048 so the 512M reservation is honored and still leaves a bootable first kernel;
    ``force_crash`` is opted in for the destructive-op gate (ADR-0045).
    """
    return {
        "schema_version": 1,
        "arch": "ppc64le",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": LOCAL_ALLOCATION_DISK_GB,
        "boot_method": "direct-kernel",
        "kernel_source_ref": os.environ[_KERNEL_TREE_ENV],
        "provider": {
            "local-libvirt": {
                "rootfs": {"kind": "local", "path": image},
                "crashkernel": "256M",
                "destructive_ops": ["force_crash"],
            }
        },
    }


def _ppc64le_fadump_provision_profile(image: str) -> dict[str, object]:
    """A ppc64le FADUMP + force_crash provision profile for the #1151 capture proof (ADR-0349).

    Mirrors the KDUMP profile but sets ``debug.fadump=True``: with the ``crashkernel`` reservation
    present, ``capture_method`` resolves to ``FADUMP`` and the boot cmdline gains ``fadump=on``
    alongside the arch-default ``crashkernel=512M``. Admission accepts it only because the live host
    QEMU (>= 10.2) advertises ``pseries_fadump`` at discovery. The ``256M`` token is the same
    method-signal sentinel the kdump proof uses (the value is never the reservation size).

    ``memory_mb`` is **4096** — the fadump RAM floor (ADR-0363, #1181), not the kdump proof's 2048.
    On POWER, fadump reserves a boot-memory region on top of crashkernel; at 2 GiB the guest never
    reaches run-readiness under native KVM (kdump on the same guest passes at 2 GiB). The paired
    ``allocations.request`` reserves ``memory_gb=4`` so the reconciled size matches this floor.
    """
    return {
        "schema_version": 1,
        "arch": "ppc64le",
        "vcpu": 2,
        "memory_mb": 4096,
        "disk_gb": LOCAL_ALLOCATION_DISK_GB,
        "boot_method": "direct-kernel",
        "kernel_source_ref": os.environ[_KERNEL_TREE_ENV],
        "provider": {
            "local-libvirt": {
                "rootfs": {"kind": "local", "path": image},
                "crashkernel": "256M",
                "debug": {"fadump": True},
                "destructive_ops": ["force_crash"],
            }
        },
    }


@pytest.mark.live_stack
@pytest.mark.live_vm_tcg
def test_ppc64le_fadump_captures_a_vmcore_under_tcg() -> None:
    """Attempt a fadump capture on a ppc64le guest under TCG on the x86_64 host (#1151, ADR-0349).

    The feasibility-gate live proof for #1151 acceptance criterion 5. Mirrors the #1148 KDUMP
    driver but provisions a **FADUMP** System (``debug.fadump=True`` + the reservation), so:

    - admission accepts fadump only because the live host QEMU (10.2.2) advertises
      ``pseries_fadump`` at discovery (the fail-closed gate, ADR-0349);
    - the boot cmdline carries **``fadump=on``** alongside the arch-default ``crashkernel=512M``
      (asserted in the running domain's ``<cmdline>`` — the divergence from the kdump proof);
    - ``control.force_crash`` panics the guest; fadump's memory-preserving reboot (or the kdump
      fallback if fadump cannot register) yields a ``/proc/vmcore`` the shared kdump userspace
      saves; ``vmcore.fetch`` harvests it under the **``vmcore-fadump``** key.

    **Native-POWER driver.** The 2026-07-14 live run (proof-record doc
    ``docs/design/2026-07-14-ppc64le-fadump-proof-record-1151.md``, ADR-0349) established that
    fadump *registers* under QEMU 10.2 TCG (``rtas fadump: Registration is successful!``) but the
    guest's periodic ``rtas_event_scan`` RTAS call then Oopses under emulation, so the guest never
    reaches readiness and the crash→capture cycle cannot complete under TCG. The crash path rides
    the same Oopsing RTAS, so no boot-window tuning recovers it. This test therefore **skips on any
    non-ppc64le host** (where a ppc64le guest necessarily runs under TCG) and serves as the driver
    for a real POWER host (KVM), where it exercises the full capture unchanged. Skips cleanly
    without ``qemu-system-ppc64`` / the rootfs / the bundle. Self-cleans (release) on exit.
    """
    if platform.machine() != "ppc64le":
        pytest.skip(
            "fadump end-to-end capture requires native POWER (KVM); under TCG the guest's RTAS "
            "fadump emulation Oopses after a successful registration, so readiness never completes "
            "(see docs/design/2026-07-14-ppc64le-fadump-proof-record-1151.md). fadump registration "
            "itself is proven under QEMU 10.2 TCG."
        )
    issuer, base_url, image, kernel_tar, initrd = _ppc64le_bundle_preflight()
    operator_token = _token(issuer, role="operator")
    admin_token = _token(issuer, role="admin")
    proof_token = f"kdive_proof_token={uuid4().hex[:12]}"

    async def _run() -> None:
        op = LiveStackClient.over_http(base_url, operator_token)
        admin = LiveStackClient.over_http(base_url, admin_token)
        allocation_id = ""
        async with op, admin:
            try:
                async with phase("ppc64le-fadump:allocate"):
                    env = ok(
                        await scalar(
                            op,
                            "allocations.request",
                            project=_PROJECT,
                            # memory_gb=4 is the fadump RAM floor (ADR-0363, #1181); the reconciled
                            # profile size must not fall below it, unlike the kdump proof's 2 GiB.
                            **{
                                "vcpus": 2,
                                "memory_gb": 4,
                                "disk_gb": LOCAL_ALLOCATION_DISK_GB,
                                "resource": {"mode": "kind"},
                            },
                        ),
                        "ppc64le-fadump:allocate",
                    )
                    allocation_id = env.object_id
                async with phase("ppc64le-fadump:provision"):
                    profile = _ppc64le_fadump_provision_profile(image)
                    env = ok(
                        await scalar(
                            op, "systems.provision", allocation_id=allocation_id, profile=profile
                        ),
                        "ppc64le-fadump:provision",
                    )
                    system_id = data_str(env, "system_id")
                    await await_system_state(op, "ppc64le-fadump:provision", system_id, "ready")
                async with phase("ppc64le-fadump:create-run"):
                    env = ok(
                        await scalar(
                            op,
                            "investigations.open",
                            **{"project": _PROJECT, "title": "ppc64le-fadump-1151"},
                        ),
                        "ppc64le-fadump:create-run",
                    )
                    env = ok(
                        await scalar(
                            op,
                            "runs.create",
                            **{
                                "investigation_id": env.object_id,
                                "system_id": system_id,
                                "build_profile": {"schema_version": 1, "arch": "ppc64le"},
                            },
                        ),
                        "ppc64le-fadump:create-run",
                    )
                    run_id = env.object_id
                async with phase("ppc64le-fadump:upload"):
                    decls = [
                        {
                            "name": "kernel",
                            "sha256": _sha256_b64(kernel_tar),
                            "size_bytes": kernel_tar.stat().st_size,
                        },
                        {
                            "name": "initrd",
                            "sha256": _sha256_b64(initrd),
                            "size_bytes": initrd.stat().st_size,
                        },
                    ]
                    up = ok(
                        await scalar(
                            op, "artifacts.create_run_upload", run_id=run_id, artifacts=decls
                        ),
                        "ppc64le-fadump:upload",
                    )
                    by_name = {item.data.get("name"): item for item in up.items}
                    await _put_presigned(by_name["kernel"], kernel_tar)
                    await _put_presigned(by_name["initrd"], initrd)
                    ok(
                        await scalar(op, "runs.complete_build", run_id=run_id),
                        "ppc64le-fadump:upload",
                    )
                async with phase("ppc64le-fadump:install"):
                    env = ok(
                        await scalar(op, "runs.install", run_id=run_id, cmdline=proof_token),
                        "ppc64le-fadump:install",
                    )
                    await drain_job(op, "ppc64le-fadump:install", env.object_id)
                async with phase("ppc64le-fadump:boot"):
                    env = ok(await scalar(op, "runs.boot", run_id=run_id), "ppc64le-fadump:boot")
                    await drain_job(
                        op,
                        "ppc64le-fadump:boot",
                        env.object_id,
                        deadline_s=_PPC64LE_BOOT_DEADLINE_S,
                    )
                async with phase("ppc64le-fadump:attribute"):
                    xml = subprocess.run(
                        ["virsh", "-c", "qemu:///system", "dumpxml", f"kdive-{system_id}"],
                        capture_output=True,
                        text=True,
                        check=True,
                    ).stdout
                    assert run_id in xml, "domain <kernel> not at the per-Run staged path"
                    assert proof_token in xml, "install cmdline token did not reach <cmdline>"
                    assert "pseries" in xml, "domain is not a pseries machine"
                    # The FADUMP divergence: fadump=on rides alongside the arch-default reservation.
                    assert "fadump=on" in xml, "fadump=on not in <cmdline> — fadump path not booted"
                    assert "crashkernel=512M" in xml, (
                        "arch-default crashkernel=512M not in <cmdline>"
                    )
                async with phase("ppc64le-fadump:crash"):
                    ok(
                        await scalar(admin, "control.force_crash", system_id=system_id),
                        "ppc64le-fadump:crash",
                    )
                    await await_system_state(admin, "ppc64le-fadump:crash", system_id, "crashed")
                async with phase("ppc64le-fadump:capture"):
                    env = ok(
                        await scalar(op, "vmcore.fetch", run_id=run_id), "ppc64le-fadump:capture"
                    )
                    await drain_job(
                        op,
                        "ppc64le-fadump:capture",
                        env.object_id,
                        deadline_s=_PPC64LE_BOOT_DEADLINE_S,
                    )
                    listing = ok(
                        await scalar(op, "vmcore.list", run_id=run_id), "ppc64le-fadump:capture"
                    )
                    refs = [v for item in listing.items for v in item.refs.values()]
                    assert refs, "no vmcore artifact listed — fadump captured no core"
                    # The core is keyed by the resolved method: vmcore-fadump (not vmcore-kdump).
                    assert any("vmcore-fadump" in r for r in refs), (
                        f"no vmcore-fadump artifact — got {refs!r}"
                    )
                    # Only the redacted core is exposed (raw vmcore-<method> must never surface).
                    assert all(
                        not ("/vmcore-" in r and not r.endswith("-redacted")) for r in refs
                    ), "raw ppc64le vmcore leaked"
            finally:
                if allocation_id:
                    await scalar(op, "allocations.release", allocation_id=allocation_id)

    asyncio.run(_run())


@pytest.mark.live_stack
@pytest.mark.live_vm_tcg
def test_ppc64le_kdump_captures_a_vmcore_under_tcg() -> None:
    """Prove kdump capture works on a ppc64le guest under TCG on the x86_64 host (#1148).

    The blocking live proof for #1148 acceptance criterion 5. Extends the #1146 uploaded-bundle
    boot through the **KDUMP** capture path:

    - ``runs.install`` on a KDUMP System fires the guest kernel writer, whose module indexing now
      runs **host-side** (``depmod -b``, ADR-0346) — so the ppc64le module tree indexes correctly
      under the x86_64 libguestfs appliance, retiring #1146's CONSTRAINED depmod verdict.
    - The boot uses the ppc64le **arch-default** ``crashkernel=512M`` (ADR-0346), not the ``256M``
      sentinel profile token and no per-install override — asserted in the running domain's
      ``<cmdline>``, so the default (not the profile/per-install value) provably sized it.
    - ``control.force_crash`` panics the guest; its kdump kernel kexec-boots, runs makedumpfile, and
      writes the vmcore; ``vmcore.fetch`` harvests it and ``vmcore.list`` surfaces a redacted core.

    The pseries VMCOREINFO/fw_cfg verdict is recorded from this run (the domain emits no
    ``<features>`` device — kdump reads VMCOREINFO from ``/proc/vmcore``, ADR-0346 §3) in the
    proof-record doc. Skips cleanly without ``qemu-system-ppc64`` / the rootfs / the bundle.
    Self-cleans (release) on exit.
    """
    issuer, base_url, image, kernel_tar, initrd = _ppc64le_bundle_preflight()
    operator_token = _token(issuer, role="operator")
    admin_token = _token(issuer, role="admin")
    proof_token = f"kdive_proof_token={uuid4().hex[:12]}"

    async def _run() -> None:
        op = LiveStackClient.over_http(base_url, operator_token)
        admin = LiveStackClient.over_http(base_url, admin_token)
        allocation_id = ""
        async with op, admin:
            try:
                async with phase("ppc64le-kdump:allocate"):
                    env = ok(
                        await scalar(
                            op,
                            "allocations.request",
                            project=_PROJECT,
                            **{
                                "vcpus": 2,
                                "memory_gb": 2,
                                "disk_gb": LOCAL_ALLOCATION_DISK_GB,
                                "resource": {"mode": "kind"},
                            },
                        ),
                        "ppc64le-kdump:allocate",
                    )
                    allocation_id = env.object_id
                async with phase("ppc64le-kdump:provision"):
                    profile = _ppc64le_kdump_provision_profile(image)
                    env = ok(
                        await scalar(
                            op, "systems.provision", allocation_id=allocation_id, profile=profile
                        ),
                        "ppc64le-kdump:provision",
                    )
                    system_id = data_str(env, "system_id")
                    await await_system_state(op, "ppc64le-kdump:provision", system_id, "ready")
                async with phase("ppc64le-kdump:create-run"):
                    env = ok(
                        await scalar(
                            op,
                            "investigations.open",
                            **{"project": _PROJECT, "title": "ppc64le-kdump-1148"},
                        ),
                        "ppc64le-kdump:create-run",
                    )
                    env = ok(
                        await scalar(
                            op,
                            "runs.create",
                            **{
                                "investigation_id": env.object_id,
                                "system_id": system_id,
                                "build_profile": {"schema_version": 1, "arch": "ppc64le"},
                            },
                        ),
                        "ppc64le-kdump:create-run",
                    )
                    run_id = env.object_id
                async with phase("ppc64le-kdump:upload"):
                    decls = [
                        {
                            "name": "kernel",
                            "sha256": _sha256_b64(kernel_tar),
                            "size_bytes": kernel_tar.stat().st_size,
                        },
                        {
                            "name": "initrd",
                            "sha256": _sha256_b64(initrd),
                            "size_bytes": initrd.stat().st_size,
                        },
                    ]
                    up = ok(
                        await scalar(
                            op, "artifacts.create_run_upload", run_id=run_id, artifacts=decls
                        ),
                        "ppc64le-kdump:upload",
                    )
                    by_name = {item.data.get("name"): item for item in up.items}
                    await _put_presigned(by_name["kernel"], kernel_tar)
                    await _put_presigned(by_name["initrd"], initrd)
                    ok(
                        await scalar(op, "runs.complete_build", run_id=run_id),
                        "ppc64le-kdump:upload",
                    )
                async with phase("ppc64le-kdump:install"):
                    # KDUMP install → the guest kernel writer indexes the ppc64le module tree
                    # host-side (ADR-0346). No per-install crashkernel → the arch default (512M).
                    env = ok(
                        await scalar(op, "runs.install", run_id=run_id, cmdline=proof_token),
                        "ppc64le-kdump:install",
                    )
                    await drain_job(op, "ppc64le-kdump:install", env.object_id)
                async with phase("ppc64le-kdump:boot"):
                    env = ok(await scalar(op, "runs.boot", run_id=run_id), "ppc64le-kdump:boot")
                    await drain_job(
                        op, "ppc64le-kdump:boot", env.object_id, deadline_s=_PPC64LE_BOOT_DEADLINE_S
                    )
                async with phase("ppc64le-kdump:attribute"):
                    xml = subprocess.run(
                        ["virsh", "-c", "qemu:///system", "dumpxml", f"kdive-{system_id}"],
                        capture_output=True,
                        text=True,
                        check=True,
                    ).stdout
                    assert run_id in xml, "domain <kernel> not at the per-Run staged path"
                    assert proof_token in xml, "install cmdline token did not reach <cmdline>"
                    assert "pseries" in xml, "domain is not a pseries machine"
                    # The arch default sized the reservation (not the 256M sentinel profile token).
                    assert "crashkernel=512M" in xml, (
                        "arch-default crashkernel=512M not in <cmdline>"
                    )
                    # kdump needs no QEMU vmcoreinfo device on pseries (ADR-0346 §3): none emitted.
                    assert "<vmcoreinfo" not in xml, (
                        "pseries domain unexpectedly emits <vmcoreinfo>"
                    )
                async with phase("ppc64le-kdump:crash"):
                    ok(
                        await scalar(admin, "control.force_crash", system_id=system_id),
                        "ppc64le-kdump:crash",
                    )
                    await await_system_state(admin, "ppc64le-kdump:crash", system_id, "crashed")
                async with phase("ppc64le-kdump:capture"):
                    env = ok(
                        await scalar(op, "vmcore.fetch", run_id=run_id), "ppc64le-kdump:capture"
                    )
                    await drain_job(
                        op,
                        "ppc64le-kdump:capture",
                        env.object_id,
                        deadline_s=_PPC64LE_BOOT_DEADLINE_S,
                    )
                    listing = ok(
                        await scalar(op, "vmcore.list", run_id=run_id), "ppc64le-kdump:capture"
                    )
                    refs = [v for item in listing.items for v in item.refs.values()]
                    assert refs, "no vmcore artifact listed — kdump captured no core"
                    # Only the redacted core is exposed (raw vmcore-<method> must never surface).
                    assert all(
                        not ("/vmcore-" in r and not r.endswith("-redacted")) for r in refs
                    ), "raw ppc64le vmcore leaked"
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
