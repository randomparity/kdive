"""Server-side ownership fence for activation-free authority Systems (ADR-0623)."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import Job, JobKind, JobState
from kdive.jobs import queue
from kdive.jobs.payloads import (
    EXTERNAL_BOOT_AUTHORITY_MARKER_KEY,
    Authorizing,
    SystemPayload,
    TeardownPayload,
    load_payload,
)
from kdive.providers.system_authority.protocol import (
    SSH_FORWARD_PORT_MAX,
    SSH_FORWARD_PORT_MIN,
    AuthoritySystemMarkerV1,
    AuthoritySystemOperation,
)

_AUTHORITY_MARKER_KEYS = frozenset({"authority_system_v1", EXTERNAL_BOOT_AUTHORITY_MARKER_KEY})
_BINDING_SQL = "SELECT * FROM resolve_authority_system_control_binding(%s)"
_OWNERSHIP_STATES = frozenset(
    {"provisioning", "ready", "teardown-requested", "torn-down", "activated", "repair-required"}
)


@dataclass(frozen=True, slots=True)
class AuthoritySystemBinding:
    """Immutable server-visible identity of one authority-owned System."""

    allocation_id: UUID
    resource_id: UUID
    provider_kind: Literal["local-libvirt", "remote-libvirt"]
    resource_name: str
    authority_instance: str
    profile_identity: str
    root_identity: str
    ownership_state: str
    ssh_forward_port: int | None = None


def _invalid_binding() -> CategorizedError:
    return CategorizedError(
        "authority-owned System binding is malformed",
        category=ErrorCategory.CONFIGURATION_ERROR,
    )


def _text(value: object) -> str:
    text = value if isinstance(value, str) else ""
    if not text or len(text.encode("utf-8")) > 255:
        raise _invalid_binding()
    return text


def _uuid(value: object) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except ValueError as exc:
        raise _invalid_binding() from exc


def _binding(row: dict[str, object]) -> AuthoritySystemBinding:
    provider_kind = _text(row.get("provider_kind"))
    ownership_state = _text(row.get("ownership_state"))
    if (
        provider_kind not in {"local-libvirt", "remote-libvirt"}
        or ownership_state not in _OWNERSHIP_STATES
    ):
        raise _invalid_binding()
    return AuthoritySystemBinding(
        allocation_id=_uuid(row.get("allocation_id")),
        resource_id=_uuid(row.get("resource_id")),
        provider_kind=provider_kind,
        resource_name=_text(row.get("resource_name")),
        authority_instance=_text(row.get("authority_instance")),
        profile_identity=_text(row.get("profile_identity")),
        root_identity=_text(row.get("root_identity")),
        ownership_state=ownership_state,
        ssh_forward_port=_forward_port(row.get("ssh_forward_port")),
    )


def _forward_port(value: object) -> int | None:
    """The reported loopback SSH port, or ``None``; a value outside the range is malformed."""
    if value is None:
        return None
    if type(value) is not int or not SSH_FORWARD_PORT_MIN <= value <= SSH_FORWARD_PORT_MAX:
        raise _invalid_binding()
    return value


async def authority_system_binding(
    conn: AsyncConnection, system_id: UUID
) -> AuthoritySystemBinding | None:
    """Read the immutable authority binding, failing closed on an invalid result."""
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(_BINDING_SQL, (system_id,))
        row = await cursor.fetchone()
    return None if row is None else _binding(dict(row))


async def authority_ssh_forward_port(conn: AsyncConnection, system_id: UUID) -> int | None:
    """The loopback SSH port a local authority-owned System reported, for every server SSH path.

    Debug attach and the SSH access tools read it here. Only a server role can: the worker is
    refused the binding, so a job that needs the port carries it.

    ``None`` means the System is not authority-owned or is not local-libvirt, so the provider
    resolves the forward itself (ADR-0811). An authority-owned local System that cannot be
    attached to raises ``CONFIGURATION_ERROR`` instead of falling back: the server's own libvirt
    endpoint cannot see the authority's domain, and a guessed port would be a forged endpoint.
    """
    binding = await authority_system_binding(conn, system_id)
    if binding is None or binding.provider_kind != "local-libvirt":
        return None
    if binding.ssh_forward_port is None:
        reason = (
            "authority_system_not_attachable"
            if binding.ownership_state not in {"ready", "activated"}
            else "authority_ssh_forward_unreported"
        )
        raise CategorizedError(
            "this authority-owned System reported no SSH forward to attach through; it was "
            "provisioned before the authority reported one, or the authority is older than this "
            "server. Reprovision it against an upgraded authority.",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": reason},
        )
    return binding.ssh_forward_port


async def ordinary_mutation_is_fenced(conn: AsyncConnection, system_id: UUID) -> bool:
    """Whether a pre-first-activation authority System refuses ordinary mutation."""
    binding = await authority_system_binding(conn, system_id)
    return binding is not None and binding.ownership_state != "activated"


async def torn_down_after_activation(conn: AsyncConnection, system_id: UUID) -> bool:
    """Whether a torn-down authority System recorded its first activation (ADR-0754).

    Reads the ownership row's first activation, not activation rows: a claim blocked by a
    requested teardown can leave a row that was never a first activation (#3423). The
    pre-activation fence does not apply to the activated case (#3382), so its allocation can be
    released.
    """
    cursor = await conn.execute(
        "SELECT authority_system_torn_down_after_activation(%s)", (system_id,)
    )
    row = await cursor.fetchone()
    return row is not None and row[0] is True


async def preactivation_absence_is_proven(conn: AsyncConnection, system_id: UUID) -> bool:
    """Whether the authority's consumed absence receipt proves a never-activated System gone.

    Only allocation release may rely on it (ADR-0754); every other mutation keeps the fence.
    """
    cursor = await conn.execute(
        "SELECT authority_system_preactivation_absence_proven(%s)", (system_id,)
    )
    row = await cursor.fetchone()
    return row is not None and row[0] is True


def authority_owned_provision_system_id(job: Job) -> UUID | None:
    """Return the System for a marked provision job, rejecting malformed authority payloads."""
    if job.kind is not JobKind.PROVISION or "authority_system_v1" not in job.payload:
        return None
    try:
        payload = load_payload(job, SystemPayload)
        marker = payload.authority_system_v1
        if marker is None:
            raise ValueError("authority marker missing")
        return UUID(payload.system_id)
    except (ValueError, TypeError) as exc:
        raise CategorizedError(
            "authority-owned provision job payload is malformed",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from exc


def authority_owned_preactivation_teardown_system_id(job: Job) -> UUID | None:
    """Return the System for an authority teardown, rejecting malformed marked payloads."""
    if job.kind is not JobKind.TEARDOWN or "authority_system_v1" not in job.payload:
        return None
    try:
        payload = load_payload(job, TeardownPayload)
        marker = payload.authority_system_v1
        if (
            marker is None
            or marker.operation is not AuthoritySystemOperation.PREACTIVATION_TEARDOWN
        ):
            raise ValueError("preactivation teardown marker missing")
        return UUID(payload.system_id)
    except (ValueError, TypeError) as exc:
        raise CategorizedError(
            "authority-owned teardown job payload is malformed",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from exc


def _teardown_dedup_key(system_id: UUID) -> str:
    return f"{system_id}:teardown"


async def _dedup_job(conn: AsyncConnection, dedup_key: str) -> Job | None:
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute("SELECT * FROM jobs WHERE dedup_key=%s", (dedup_key,))
        row = await cursor.fetchone()
    return Job.model_validate(row) if row is not None else None


async def enqueue_preactivation_teardown(
    conn: AsyncConnection,
    system: System,
    binding: AuthoritySystemBinding,
    authorizing: Authorizing,
    *,
    recycle: queue.JobRecyclePolicy,
) -> Job:
    """Enqueue the sole authority teardown and advance its ownership state atomically.

    The caller holds the System transaction lock. A dedup collision with any ordinary teardown is
    a conflict: an authority-owned System must never let a worker dispatch an unmarked teardown.
    A ``failed`` prior job with the same marker (its retained-receipt budget is spent, #3530) is
    restarted only when ``recycle`` admits ``failed`` rows, i.e. on an operator request; automatic
    callers pass ``NEVER`` so the bound cannot be re-spent every sweep.
    """
    dedup_key = _teardown_dedup_key(system.id)
    operation_identity = (
        "sha256:"
        + sha256(
            b"kdive-authority-system-preactivation-teardown-v1\0"
            + system.id.bytes
            + dedup_key.encode("utf-8")
        ).hexdigest()
    )
    marker = AuthoritySystemMarkerV1.model_validate(
        {
            "system_id": system.id,
            "allocation_id": binding.allocation_id,
            "resource_id": binding.resource_id,
            "provider_kind": binding.provider_kind,
            "resource_name": binding.resource_name,
            "authority_instance": binding.authority_instance,
            "profile_identity": binding.profile_identity,
            "root_identity": binding.root_identity,
            "operation": AuthoritySystemOperation.PREACTIVATION_TEARDOWN,
            "operation_identity": operation_identity,
        }
    )
    prior = await _dedup_job(conn, dedup_key)
    if prior is not None and prior.payload.get("authority_system_v1") != marker.model_dump(
        mode="json", by_alias=True
    ):
        raise CategorizedError(
            "an ordinary teardown job cannot be replayed for an authority-owned System",
            category=ErrorCategory.CONFLICT,
        )
    if prior is not None and prior.state is not JobState.FAILED:
        job = prior
    else:
        job = await queue.enqueue(
            conn,
            JobKind.TEARDOWN,
            TeardownPayload(system_id=str(system.id), authority_system_v1=marker),
            authorizing,
            dedup_key,
            recycle=recycle,
        )
    result = await (
        await conn.execute(
            "SELECT request_authority_system_preactivation_teardown(%s,%s,%s)",
            (system.id, job.id, operation_identity),
        )
    ).fetchone()
    if result is None or result[0] not in {"applied", "replay"}:
        raise CategorizedError(
            "authority-owned System teardown admission conflicted",
            category=ErrorCategory.CONFLICT,
        )
    return job


async def enqueue_control_teardown(
    conn: AsyncConnection,
    system: System,
    authorizing: Authorizing,
    *,
    recycle: queue.JobRecyclePolicy,
) -> Job:
    """Route a System-locked control teardown through its current ownership contract.

    ``recycle`` governs the ordinary ``{uid}:teardown`` row only; each caller states its policy
    (ADR-0435). The preactivation-authority branch restarts a ``failed`` same-marker row only when
    it admits one; any other authority-marked prior row replays (#2917, ADR-0620).
    """
    binding = await authority_system_binding(conn, system.id)
    if binding is not None and binding.ownership_state != "activated":
        return await enqueue_preactivation_teardown(
            conn, system, binding, authorizing, recycle=recycle
        )
    dedup_key = _teardown_dedup_key(system.id)
    if recycle is not queue.JobRecyclePolicy.NEVER:
        prior = await _dedup_job(conn, dedup_key)
        if prior is not None and _AUTHORITY_MARKER_KEYS & prior.payload.keys():
            recycle = queue.JobRecyclePolicy.NEVER
    return await queue.enqueue(
        conn,
        JobKind.TEARDOWN,
        TeardownPayload(system_id=str(system.id)),
        authorizing,
        dedup_key,
        recycle=recycle,
    )
