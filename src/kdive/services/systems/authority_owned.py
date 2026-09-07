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
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, SystemPayload, TeardownPayload, load_payload
from kdive.providers.system_authority.protocol import (
    AuthoritySystemMarkerV1,
    AuthoritySystemOperation,
)

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
    )


async def authority_system_binding(
    conn: AsyncConnection, system_id: UUID
) -> AuthoritySystemBinding | None:
    """Read the immutable authority binding, failing closed on an invalid result."""
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(_BINDING_SQL, (system_id,))
        row = await cursor.fetchone()
    return None if row is None else _binding(dict(row))


async def ordinary_mutation_is_fenced(conn: AsyncConnection, system_id: UUID) -> bool:
    """Whether a pre-first-activation authority System refuses ordinary mutation."""
    binding = await authority_system_binding(conn, system_id)
    return binding is not None and binding.ownership_state != "activated"


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
) -> Job:
    """Enqueue the sole authority teardown and advance its ownership state atomically.

    The caller holds the System transaction lock. A dedup collision with any ordinary teardown is
    a conflict: an authority-owned System must never let a worker dispatch an unmarked teardown.
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
    if prior is not None:
        if prior.payload.get("authority_system_v1") != marker.model_dump(
            mode="json", by_alias=True
        ):
            raise CategorizedError(
                "an ordinary teardown job cannot be replayed for an authority-owned System",
                category=ErrorCategory.CONFLICT,
            )
        job = prior
    else:
        job = await queue.enqueue(
            conn,
            JobKind.TEARDOWN,
            TeardownPayload(system_id=str(system.id), authority_system_v1=marker),
            authorizing,
            dedup_key,
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
) -> Job:
    """Route a System-locked control teardown through its current ownership contract."""
    binding = await authority_system_binding(conn, system.id)
    if binding is not None and binding.ownership_state != "activated":
        return await enqueue_preactivation_teardown(conn, system, binding, authorizing)
    return await queue.enqueue(
        conn,
        JobKind.TEARDOWN,
        TeardownPayload(system_id=str(system.id)),
        authorizing,
        _teardown_dedup_key(system.id),
    )
