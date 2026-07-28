"""Shared, provider-agnostic live-stack spine scaffolding (ADR-0042 §4/§5, ADR-0045/0046).

The local-libvirt spine (``test_live_stack.py``) and the remote-libvirt spine
(``test_remote_live_stack.py``) drive the same shape — allocate → … → release → teardown →
report — over the live MCP HTTP transport. The contract they share lives here so a fix to the
phase-naming, drain/state-polling, out-of-band DB seeding, or accounting-report assertions lands
in one place: the phase-naming contract (``phase`` / ``SpinePhaseError``), the envelope helpers
(``ok`` / ``scalar``), the async-drain helpers (``drain_job`` / ``await_system_state`` — both with
an overridable deadline so a longer phase can extend its budget), the per-project role-token
factory (``mint_role_token``), the out-of-band metering/capability seeders, and the audit / ledger
/ report helpers. Provider-specific pieces (profile factories, the booted-spine bodies, the
owned-infra teardown check) stay in each spine's own module.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg

from kdive.domain.accounting.cost import quantize_kcu
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer, mint_token
from kdive.mcp.resources.external_build_contract import EXTERNAL_BUILD_CONTRACT_URI
from kdive.mcp.responses import JsonValue, ToolResponse
from tests.mcp.json_data import data_str

# Above the 300s jobs.wait cap and the 30s reconciler interval; teardown is the slowest phase.
DRAIN_DEADLINE_S = 600.0
POLL_INTERVAL_S = 2.0

# An allocation's disk request and its provision profile's disk_gb must agree exactly, or
# reconcile_profile_sizing rejects the mismatch before provision runs (#315/#656, ADR-0205).
# One source per spine keeps the allocate request and the profile factory from drifting apart.
REMOTE_ALLOCATION_DISK_GB = 10
LOCAL_ALLOCATION_DISK_GB = 10

_ARTIFACT_DIR_ENV = "KDIVE_ARTIFACT_DIR"


# --- phase-failure naming contract (ADR-0042 §4, ADR-0045 §2) -------------------------------


class SpinePhaseError(AssertionError):
    """A spine phase failed; carries the phase name so a failure says which step died.

    The rendered message includes ``error_category`` when the envelope carried one. Without it
    a failing phase reads only "error envelope", which names the step but not the fault — every
    diagnosis then costs a re-run with an ad-hoc probe to recover the category the spine already
    had in hand.
    """

    def __init__(self, phase: str, reason: str, *, error_category: str | None = None) -> None:
        self.phase = phase
        self.reason = reason
        self.error_category = error_category
        detail = f"{reason} ({error_category})" if error_category else reason
        super().__init__(f"phase {phase!r} failed: {detail}")


@asynccontextmanager
async def phase(name: str) -> AsyncIterator[None]:
    """Run a phase; convert any failure into a ``SpinePhaseError`` naming the phase."""
    try:
        yield
    except SpinePhaseError:
        raise  # an inner phase already named itself; do not re-wrap
    except Exception as exc:  # noqa: BLE001 — deliberately broad: every failure names its phase
        raise SpinePhaseError(name, str(exc)) from exc


# --- envelope-assert helpers ----------------------------------------------------------------


def ok(envelope: ToolResponse, phase_name: str) -> ToolResponse:
    """Return the envelope if non-failure, else raise a SpinePhaseError naming the phase."""
    if envelope.status in {"error", "failed"}:
        raise SpinePhaseError(
            phase_name, f"{envelope.status} envelope", error_category=envelope.error_category
        )
    return envelope


async def scalar(client: LiveStackClient, name: str, **args: object) -> ToolResponse:
    """Call a scalar tool and narrow the result to a single ``ToolResponse``."""
    env = await client.call_tool(name, **args)
    assert isinstance(env, ToolResponse), f"{name} returned a list, expected one envelope"
    return env


# --- async-drain helpers (ADR-0045 §2) ------------------------------------------------------


async def drain_job(
    client: LiveStackClient,
    phase_name: str,
    job_id: str,
    *,
    deadline_s: float = DRAIN_DEADLINE_S,
) -> ToolResponse:
    """Poll jobs.wait until the job succeeds; classify the three outcomes (ADR-0045 §2).

    ``deadline_s`` is overridable so a longer phase (the remote two-phase capture, which waits
    out a server-side readiness window) can extend the drain budget.
    """
    deadline = time.monotonic() + deadline_s
    while True:
        env = await client.call_tool("jobs.wait", job_id=job_id, timeout_s=60.0)
        assert isinstance(env, ToolResponse)
        if env.status == "succeeded":
            return env
        if env.status in {"failed", "canceled"}:
            raise SpinePhaseError(
                phase_name, f"job {env.status}", error_category=env.error_category
            )
        if time.monotonic() >= deadline:  # non-terminal return: a worker stall
            raise SpinePhaseError(phase_name, "drain_timeout")
        await asyncio.sleep(POLL_INTERVAL_S)


async def captured_vmcore_refs(
    client: LiveStackClient,
    phase_name: str,
    drained: ToolResponse,
    *,
    run_id: str,
) -> list[str]:
    """Resolve a drained capture job's published core reference to its refs (ADR-0466).

    Replaces the retired ``vmcore.list`` sweep. The terminal ``capture_vmcore`` job carries the
    redacted core's artifact id in ``refs.result``, and ``runs.get`` carries the same id as
    ``refs.vmcore``; both are read and required to agree, so the run-keyed lookup path an agent
    falls back to is proven live and not just in unit tests. ``artifacts.get`` then resolves the
    stored object key, keeping the caller's method-provenance and raw-leak assertions on real
    evidence rather than on an opaque id.

    Returns every ref value collected along the way, for the caller to assert over.
    """
    job_ref = drained.refs.get("result")
    if not job_ref:
        raise SpinePhaseError(phase_name, "capture job published no vmcore reference")
    run_env = ok(await scalar(client, "runs.get", run_id=run_id), phase_name)
    if run_env.refs.get("vmcore") != job_ref:
        raise SpinePhaseError(
            phase_name,
            f"runs.get refs.vmcore {run_env.refs.get('vmcore')!r} != job refs.result {job_ref!r}",
        )
    got = ok(await scalar(client, "artifacts.get", request={"artifact_id": job_ref}), phase_name)
    return [job_ref, *got.refs.values()]


async def await_system_state(
    client: LiveStackClient,
    phase_name: str,
    system_id: str,
    target: str,
    *,
    deadline_s: float = DRAIN_DEADLINE_S,
) -> None:
    """Poll systems.get until the System reaches ``target`` state (or the deadline)."""
    deadline = time.monotonic() + deadline_s
    while True:
        env = await client.call_tool("systems.get", system_id=system_id)
        assert isinstance(env, ToolResponse)
        if env.status == target:
            return
        if env.status in {"error", "failed"}:
            raise SpinePhaseError(
                phase_name, f"system {env.status}", error_category=env.error_category
            )
        if time.monotonic() >= deadline:
            raise SpinePhaseError(phase_name, f"system did not reach {target}")
        await asyncio.sleep(POLL_INTERVAL_S)


# --- external-build upload lane (ADR-0234; replaces the removed runs.build server lane) -----
#
# Both spines reach a bootable Run the same way: cut the combined ``kernel`` tar from a *built*
# tree, declare + PUT it through ``artifacts.create_run_upload``, then ``runs.complete_build``.
# ``runs.build`` was deleted with the server-build tables (schema 0062) — the local spine was
# migrated then, the remote spine was not, which is why the remote arm could never reach its
# capture assertions. Shared here so the next lane change lands in one place.

KERNEL_TREE_ENV = "KDIVE_KERNEL_SRC"


def sha256_b64(path: Path) -> str:
    """The base64 SHA-256 the upload contract declares (S3 ``x-amz-checksum-sha256``)."""
    return base64.b64encode(hashlib.sha256(path.read_bytes()).digest()).decode()


async def put_presigned(item: ToolResponse, path: Path) -> None:
    """PUT ``path`` to a ``create_run_upload`` item's presigned URL + required headers."""
    url = item.refs["upload_url"]
    raw_headers = item.data.get("required_headers", {})
    headers = {k: str(v) for k, v in raw_headers.items()} if isinstance(raw_headers, dict) else {}
    async with httpx.AsyncClient(timeout=180.0) as http:
        resp = await http.put(url, content=path.read_bytes(), headers=headers)
        resp.raise_for_status()


def accepted_run_upload_names(contract: dict[str, object]) -> set[str]:
    """The ``run`` owner-kind's accepted names from the external-build contract resource."""
    upload_contracts = contract.get("upload_contracts", {})
    run_contract = upload_contracts.get("run", {}) if isinstance(upload_contracts, dict) else {}
    if isinstance(run_contract, dict):
        names = run_contract.get("accepted_names", [])
        return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()
    return set()


def elf_build_id(elf: Path) -> str:
    """The GNU build-id of ``elf`` as lowercase hex, via ``readelf -n``.

    ``runs.complete_build`` requires this whenever a ``vmlinux`` is uploaded — it is how the
    debug tier matches debuginfo to the running kernel, and a mismatched id is worse than none.

    Uses ``readelf`` rather than scanning for the note by hand: ``vmlinux`` embeds several
    ``GNU`` notes (the vDSO images carry their own build-ids), so a naive scan returns whichever
    it meets first — a wrong id that still looks well-formed. The module already shells out to
    ``make`` and ``tar``, so binutils is no new obligation for a kernel-building host.

    Raises:
        RuntimeError: If ``readelf`` reports no build-id for the file.
    """
    out = subprocess.run(
        ["readelf", "-n", str(elf)], check=True, capture_output=True, text=True
    ).stdout
    for line in out.splitlines():
        marker = "Build ID:"
        if marker in line:
            return line.split(marker, 1)[1].strip().lower()
    raise RuntimeError(f"no GNU build-id note in {elf}")


def boot_member_source(kernel_src: Path, arch: str) -> Path:
    """Resolve the tree-relative path of the file that becomes the tar's ``boot/vmlinuz``.

    The ``kernel-build-per-arch`` resource is the contract this must match:

    | ``arch``  | ``boot/vmlinuz`` must be                                        |
    |-----------|-----------------------------------------------------------------|
    | ``x86_64``| the bzImage, ``arch/x86/boot/bzImage`` (validator checks ``HdrS``|
    |           | magic at offset ``0x202``; a raw ``vmlinux`` ELF is rejected)    |
    | ``ppc64le``| the stripped ELF ``vmlinux`` (powerpc has no bzImage)          |

    Args:
        kernel_src: A *built* kernel tree.
        arch: The target arch (``x86_64`` / ``ppc64le``), NOT the build host's arch.

    Returns:
        The path relative to ``kernel_src`` of the boot member to tar.

    Raises:
        RuntimeError: If ``arch`` is unsupported, or the resolved file is not present.
    """
    # Raise rather than defaulting to x86_64: a new arch silently tarring a bzImage path that
    # does not exist would surface as an opaque tar failure, or worse as a validator rejection
    # far downstream. An unknown arch is a test-setup bug, so name it here.
    members = {"x86_64": Path("arch/x86/boot/bzImage"), "ppc64le": Path("vmlinux")}
    member = members.get(arch)
    if member is None:
        raise RuntimeError(f"no boot member mapping for arch {arch!r}; known: {sorted(members)}")
    if not (kernel_src / member).is_file():
        raise RuntimeError(
            f"no built {member} at {kernel_src / member}; build the kernel tree at {kernel_src} "
            f"for {arch} first"
        )
    return member


def combined_kernel_tar(kernel_src: Path, dest_dir: Path, *, arch: str = "x86_64") -> Path:
    """Cut the ADR-0234 combined ``kernel`` artifact from a built kernel tree.

    Reproduces the ``external-build-upload`` recipe: stage the module tree with
    ``make modules_install`` into ``dest_dir``, then tar the arch's boot member (renamed to
    ``boot/vmlinuz``, listed FIRST so ``lib/modules`` lands inside the validator's
    decompress-scan bound) plus ``lib/modules`` into one gzip tar, dropping the
    ``build``/``source`` back-symlinks.

    The rename transform is derived from :func:`boot_member_source`, so that function stays the
    single place the per-arch boot member is decided.

    Args:
        kernel_src: A *built* kernel tree for ``arch``.
        dest_dir: A scratch directory to stage modules and write ``kernel.tar.gz`` into.
        arch: The target arch, passed through to :func:`boot_member_source`.

    Returns:
        The path to the combined ``kernel.tar.gz`` under ``dest_dir``.
    """
    member = boot_member_source(kernel_src, arch)
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
            f"--transform=s|^{member}$|boot/vmlinuz|",
            "-C",
            str(kernel_src),
            str(member),
            "-C",
            str(modstage),
            "lib/modules",
        ],
        check=True,
    )
    return tar_path


async def build_and_upload_kernel(
    client: LiveStackClient,
    *,
    run_id: str,
    phase_name: str = "upload-build",
    arch: str = "x86_64",
    with_vmlinux: bool = False,
) -> None:
    """Drive the external-build upload lane for ``run_id`` and complete the Run's build step.

    Reads the contract resource, cuts the combined ``kernel`` tar from ``KDIVE_KERNEL_SRC``,
    declares + PUTs it via ``artifacts.create_run_upload``, then calls ``runs.complete_build``.
    The Run goes CREATED → SUCCEEDED with ``steps.build == succeeded``, ready for ``runs.install``.

    ``with_vmlinux`` additionally uploads the tree's unstripped ``vmlinux``. The gdb-MI tier
    resolves its symbols from that artifact, so a spine that opens a debug session needs it:
    without it ``debug.read_registers`` fails ``configuration_error`` / ``no_debuginfo``. It is
    opt-in because the ELF is large (hundreds of MB) and a spine that never attaches pays the
    upload for nothing.
    """
    contract = json.loads(await client.read_text_resource(EXTERNAL_BUILD_CONTRACT_URI))
    accepted = accepted_run_upload_names(contract)
    if "kernel" not in accepted:
        raise SpinePhaseError(phase_name, f"upload contract no longer accepts 'kernel': {accepted}")
    if with_vmlinux and "vmlinux" not in accepted:
        raise SpinePhaseError(phase_name, f"upload contract accepts no 'vmlinux': {accepted}")
    kernel_src = os.environ.get(KERNEL_TREE_ENV)
    if not kernel_src:
        raise SpinePhaseError(phase_name, f"{KERNEL_TREE_ENV} unset; point it at a built tree")
    with tempfile.TemporaryDirectory(prefix="kdive-spine-kernel-") as scratch:
        kernel_tar = combined_kernel_tar(Path(kernel_src), Path(scratch), arch=arch)
        decls = [
            {
                "name": "kernel",
                "sha256": sha256_b64(kernel_tar),
                "size_bytes": kernel_tar.stat().st_size,
            }
        ]
        vmlinux = Path(kernel_src) / "vmlinux"
        if with_vmlinux:
            if not vmlinux.is_file():
                raise SpinePhaseError(phase_name, f"no unstripped vmlinux at {vmlinux}")
            decls.append(
                {
                    "name": "vmlinux",
                    "sha256": sha256_b64(vmlinux),
                    "size_bytes": vmlinux.stat().st_size,
                }
            )
        up = ok(
            await scalar(client, "artifacts.create_run_upload", run_id=run_id, artifacts=decls),
            phase_name,
        )
        by_name = {item.data.get("name"): item for item in up.items}
        if "kernel" not in by_name:
            raise SpinePhaseError(phase_name, "create_run_upload returned no 'kernel' item")
        await put_presigned(by_name["kernel"], kernel_tar)
        if with_vmlinux:
            if "vmlinux" not in by_name:
                raise SpinePhaseError(phase_name, "create_run_upload returned no 'vmlinux' item")
            await put_presigned(by_name["vmlinux"], vmlinux)
            build_id = elf_build_id(vmlinux)
    # complete_build requires build_id iff a vmlinux was uploaded; sending it otherwise (or
    # omitting it here) is a configuration_error.
    extra: dict[str, JsonValue] = {"build_id": build_id} if with_vmlinux else {}
    ok(await scalar(client, "runs.complete_build", run_id=run_id, **extra), phase_name)


# --- per-role token factory -----------------------------------------------------------------


def mint_role_token(
    issuer: OidcIssuer,
    *,
    project: str,
    agent_session: str,
    role: str,
    platform_roles: list[str] | None = None,
) -> str:
    """Mint a per-project role token (the local test's ``_token``, parameterized by project)."""
    return mint_token(
        issuer,
        subject=f"{role}-{project}",
        projects=[project],
        roles={project: role},
        platform_roles=platform_roles,
        agent_session=agent_session,
    )


# --- metering seed (ADR-0046 §0) -----------------------------------------------------------


async def seed_metering(
    db_url: str,
    project: str,
    *,
    limit_kcu: str = "1000000",
    max_allocations: int = 4,
    max_systems: int = 4,
) -> None:
    """Seed the budget (limit-only) + quota rows admission requires, out of band.

    The budget upsert writes ``limit_kcu`` only and leaves ``spent_kcu`` untouched (matching
    production ``set_budget`` / ``BUDGETS.upsert``), so a re-run of the fixed-constant project
    keeps the DB-maintained running total consistent with the ledger Σ; a first insert starts it
    at 0. Both upserts are idempotent on the ``project`` primary key.
    """
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.execute(
            "INSERT INTO budgets (project, limit_kcu) VALUES (%s, %s) "
            "ON CONFLICT (project) DO UPDATE SET limit_kcu = EXCLUDED.limit_kcu",
            (project, limit_kcu),
        )
        await conn.execute(
            "INSERT INTO quotas (project, max_concurrent_allocations, max_concurrent_systems) "
            "VALUES (%s, %s, %s) ON CONFLICT (project) DO UPDATE SET "
            "max_concurrent_allocations = EXCLUDED.max_concurrent_allocations, "
            "max_concurrent_systems = EXCLUDED.max_concurrent_systems",
            (project, max_allocations, max_systems),
        )
        await conn.commit()


# --- allocate / provision / crash phase helpers ---------------------------------------------


async def allocate_remote(
    client: LiveStackClient,
    *,
    project: str,
    phase_name: str,
) -> str:
    """Request a remote-libvirt allocation; return its id.

    force_crash on the resulting System is gated by the System's profile opt-in
    (``destructive_ops``) plus the caller's role — no out-of-band capability grant (ADR-0130).
    """
    env = ok(
        await scalar(
            client,
            "allocations.request",
            project=project,
            **{
                "vcpus": 2,
                "memory_gb": 2,
                "disk_gb": REMOTE_ALLOCATION_DISK_GB,
                "resource": {"mode": "kind", "kind": "remote-libvirt"},
            },
        ),
        phase_name,
    )
    return env.object_id


async def provision_to_ready(
    client: LiveStackClient,
    *,
    allocation_id: str,
    profile: dict[str, object],
    phase_name: str,
) -> str:
    """Provision a System from an allocation and wait for it to reach ``ready``; return its id."""
    env = ok(
        await scalar(
            client,
            "systems.provision",
            allocation_id=allocation_id,
            profile=profile,
        ),
        phase_name,
    )
    system_id = data_str(env, "system_id")  # in data, NOT object_id (the job id)
    await await_system_state(client, phase_name, system_id, "ready")
    return system_id


async def crash_to_crashed(
    admin: LiveStackClient,
    *,
    system_id: str,
    phase_name: str,
) -> None:
    """Force-crash a System (admin scope) and wait for it to reach ``crashed``."""
    ok(await scalar(admin, "control.force_crash", system_id=system_id), phase_name)
    await await_system_state(admin, phase_name, system_id, "crashed")


# --- report-phase DB + artifact helpers (ADR-0046 §0/§2/§3) ---------------------------------


async def db_now(db_url: str) -> datetime:
    """Read the Postgres server clock, so the report window shares one clock with ledger.ts."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute("SELECT now()")
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("SELECT now() returned no row")
    return row[0]


async def ledger_sums(db_url: str, project: str, since: datetime) -> tuple[Decimal, Decimal]:
    """Return ``(reserved, reconciled)`` ledger kcu_delta sums for ``project`` over ``ts >= since``.

    Quantized via the domain ``quantize_kcu`` so the DB cross-check compares like-for-like with
    the wire rollup (which the tool also quantizes).
    """
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT "
            "COALESCE(SUM(kcu_delta) FILTER (WHERE event_type = 'reserved'), 0), "
            "COALESCE(SUM(kcu_delta) FILTER (WHERE event_type = 'reconciled'), 0) "
            "FROM ledger WHERE project = %s AND ts >= %s",
            (project, since),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("ledger sum query returned no row")
    return quantize_kcu(Decimal(row[0])), quantize_kcu(Decimal(row[1]))


async def count_audit(db_url: str, *, object_id: str, transition: str, principal: str) -> int:
    """Count audit_log rows for a transition on an object under a given principal."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM audit_log "
            "WHERE object_id = %s AND transition = %s AND principal = %s",
            (object_id, transition, principal),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def count_audit_suffix(db_url: str, *, object_id: str, suffix: str, principal: str) -> int:
    """Count audit_log rows whose transition ends with ``suffix`` (robust to the prior state).

    The teardown handler writes ``f"{old.value}->torn_down"``; the prior state depends on the
    spine, so match the ``->torn_down`` suffix rather than a fixed literal.
    """
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM audit_log "
            "WHERE object_id = %s AND transition LIKE %s AND principal = %s",
            (object_id, f"%{suffix}", principal),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def system_torn_down(db_url: str, system_id: str) -> bool:
    """True iff the System row is ``torn_down`` (the DB half of the #5 teardown check)."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
        row = await cur.fetchone()
    return row is not None and row[0] == "torn_down"


def report_artifact_dir() -> Path:
    """Resolve the artifact dir: ``KDIVE_ARTIFACT_DIR`` or an out-of-tree temp default.

    The default lives under ``tempfile.gettempdir()`` (never inside the repo) so a live run does
    not dirty the working tree or get walked by whole-tree tooling (ADR-0046 §3).
    """
    override = os.environ.get(_ARTIFACT_DIR_ENV)
    base = (
        Path(override) if override else Path(tempfile.gettempdir()) / "kdive-live-stack-artifacts"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def write_report_artifact(payload: dict[str, object], *, name: str) -> Path:
    """Write the report payload as ``name`` under the artifact dir; return its path."""
    path = report_artifact_dir() / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def find_project_row(rows: list[dict[str, JsonValue]], project: str) -> dict[str, JsonValue]:
    """Return the rollup row for ``project``, or fail the phase if absent (no spend rolled up)."""
    for row in rows:
        if row.get("project") == project:
            return row
    raise AssertionError(f"no rollup row for project {project!r} (no spend in the window?)")


async def assert_audit(db_url: str, *, project: str, allocation_id: str, system_id: str) -> None:
    """#2: audit per transition + force_crash, split by attributing principal."""
    assert (
        await count_audit(
            db_url,
            object_id=system_id,
            transition="crashing->crashed",
            principal=f"admin-{project}",
        )
        == 1
    ), "force_crash not audited under admin (#2)"
    assert (
        await count_audit(
            db_url,
            object_id=allocation_id,
            transition="releasing->released",
            principal=f"operator-{project}",
        )
        >= 1
    ), "release not audited under operator (#2)"
    # teardown is enqueued + audited by the reconciler (ADR-0021), under system:reconciler, NOT
    # the driver. The handler writes f"{old.value}->torn_down"; the spine crashed first, so the
    # row is crashed->torn_down — match the suffix to stay robust to the prior state.
    assert (
        await count_audit_suffix(
            db_url, object_id=system_id, suffix="->torn_down", principal="system:reconciler"
        )
        >= 1
    ), "teardown not audited under system:reconciler (#2)"


async def assert_report(
    base_url: str,
    auditor_token: str,
    db_url: str,
    window_start: datetime,
    *,
    project: str,
    artifact_name: str,
) -> None:
    """Drive accounting.report at all-projects scope under platform_auditor; assert spend.

    Asserts the ``project`` rollup row reflects this run's real spend (windowed wire rollup ==
    windowed DB ledger sums), then emits + re-asserts the JSON report artifact (ADR-0046 §2/§3).
    """
    auditor = LiveStackClient.over_http(base_url, auditor_token)
    async with auditor:
        env = ok(
            await scalar(
                auditor,
                "accounting.report",
                request={
                    "scope": "all-projects",
                    "window": [window_start.isoformat(), None],
                },
            ),
            "report",
        )
    rows = [item.data for item in env.items]
    total = {
        "reserved": env.data["total_reserved"],
        "reconciled": env.data["total_reconciled"],
        "variance": env.data["total_variance"],
    }
    row = find_project_row(rows, project)
    reserved = Decimal(str(row["reserved"]))
    reconciled = Decimal(str(row["reconciled"]))
    variance = Decimal(str(row["variance"]))
    assert reserved > 0, "report shows no reserved spend for the run (#101)"
    assert variance == reconciled - reserved, "report variance != reconciled - reserved (#101)"
    db_reserved, db_reconciled = await ledger_sums(db_url, project, window_start)
    assert reserved == db_reserved, f"wire reserved {reserved} != DB {db_reserved} (#101)"
    assert reconciled == db_reconciled, f"wire reconciled {reconciled} != DB {db_reconciled} (#101)"
    artifact = write_report_artifact(
        {
            "scope": env.data["scope"],
            "window": [window_start.isoformat(), None],
            "project_row": row,
            "total": total,
        },
        name=artifact_name,
    )
    assert artifact.exists(), f"report artifact not written at {artifact} (#101)"
    written = json.loads(artifact.read_text())
    project_row = written["project_row"]
    assert Decimal(str(project_row["reserved"])) == reserved, "artifact reserved drift (#101)"
    assert Decimal(str(project_row["reconciled"])) == reconciled, "artifact reconciled drift (#101)"
    assert Decimal(str(project_row["variance"])) == variance, "artifact variance drift (#101)"
