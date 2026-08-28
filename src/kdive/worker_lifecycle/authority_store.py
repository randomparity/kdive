"""Role-gated registration and authentication for exact worker incarnations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, cast, overload

from psycopg import AsyncConnection, errors
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.db.locks import require_top_level_transaction
from kdive.worker_lifecycle.contracts import TerminationOutcome

type AuthorityKind = Literal["local", "docker", "kubernetes"]


class LocalAuthorityBinding(TypedDict):
    unit: str
    generation: str
    boot_id: str
    invocation_id: str
    host: str


class DockerAuthorityBinding(TypedDict):
    container_id: str


class KubernetesAuthorityBinding(TypedDict):
    namespace: str
    name: str
    uid: str


type AuthorityBinding = LocalAuthorityBinding | DockerAuthorityBinding | KubernetesAuthorityBinding

CURRENT_WORKER_FENCE_PROTOCOL = 4


class IncarnationConflict(RuntimeError):
    """An immutable incarnation was replayed with conflicting facts."""


class IncarnationAuthenticationError(RuntimeError):
    """A credential did not identify an active worker incarnation."""


@dataclass(frozen=True, slots=True)
class WorkerIncarnation:
    """Public immutable facts for one authority-registered incarnation."""

    incarnation: str
    authority_kind: AuthorityKind
    authority_binding: AuthorityBinding
    fence_protocol: int


_BINDING_KEYS: dict[AuthorityKind, frozenset[str]] = {
    "local": frozenset(LocalAuthorityBinding.__required_keys__),
    "docker": frozenset(DockerAuthorityBinding.__required_keys__),
    "kubernetes": frozenset(KubernetesAuthorityBinding.__required_keys__),
}


def _validated_binding(kind: AuthorityKind, value: object) -> AuthorityBinding:
    if not isinstance(value, Mapping) or set(value) != _BINDING_KEYS[kind]:
        raise RuntimeError(f"worker incarnation has invalid {kind} authority binding")
    valid_values = all(
        isinstance(key, str) and isinstance(item, str) and item for key, item in value.items()
    )
    if not valid_values:
        raise RuntimeError(f"worker incarnation has invalid {kind} authority binding")
    return cast(AuthorityBinding, dict(value))


def _record(row: tuple[Any, ...]) -> WorkerIncarnation:
    authority_kind = cast(AuthorityKind, row[1])
    if authority_kind not in _BINDING_KEYS:
        raise RuntimeError("worker incarnation has invalid authority kind")
    return WorkerIncarnation(
        incarnation=cast(str, row[0]),
        authority_kind=authority_kind,
        authority_binding=_validated_binding(authority_kind, row[2]),
        fence_protocol=cast(int, row[3]),
    )


@overload
async def register_worker_incarnation(
    conn: AsyncConnection,
    incarnation: str,
    authority_kind: Literal["local"],
    binding: LocalAuthorityBinding,
    credential_hash: bytes,
    fence_protocol: int,
) -> WorkerIncarnation: ...


@overload
async def register_worker_incarnation(
    conn: AsyncConnection,
    incarnation: str,
    authority_kind: Literal["docker"],
    binding: DockerAuthorityBinding,
    credential_hash: bytes,
    fence_protocol: int,
) -> WorkerIncarnation: ...


@overload
async def register_worker_incarnation(
    conn: AsyncConnection,
    incarnation: str,
    authority_kind: Literal["kubernetes"],
    binding: KubernetesAuthorityBinding,
    credential_hash: bytes,
    fence_protocol: int,
) -> WorkerIncarnation: ...


async def register_worker_incarnation(
    conn: AsyncConnection,
    incarnation: str,
    authority_kind: AuthorityKind,
    binding: AuthorityBinding,
    credential_hash: bytes,
    fence_protocol: int,
) -> WorkerIncarnation:
    """Ask the lifecycle-witness authority to register immutable incarnation facts."""
    binding = _validated_binding(authority_kind, binding)
    require_top_level_transaction(conn, "register_worker_incarnation")
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT public.register_worker_incarnation(%s, %s, %s, %s, %s)",
                (
                    incarnation,
                    authority_kind,
                    Jsonb(binding),
                    credential_hash,
                    fence_protocol,
                ),
            )
    except errors.UniqueViolation as exc:
        raise IncarnationConflict(
            "worker incarnation registration conflicts with durable state"
        ) from exc
    return WorkerIncarnation(
        incarnation=incarnation,
        authority_kind=authority_kind,
        authority_binding=binding,
        fence_protocol=fence_protocol,
    )


async def authenticate_worker_incarnation(
    conn: AsyncConnection, credential: SecretStr
) -> WorkerIncarnation:
    """Authenticate an active identity without accepting caller-supplied holder facts."""
    require_top_level_transaction(conn, "authenticate_worker_incarnation")
    async with conn.transaction():
        row = await (
            await conn.execute(
                "SELECT incarnation, authority_kind, authority_binding, fence_protocol "
                "FROM public.authenticate_worker_incarnation("
                "sha256(convert_to(%s, 'UTF8')))",
                (credential.get_secret_value(),),
            )
        ).fetchone()
    if row is None:
        raise IncarnationAuthenticationError(
            "worker incarnation credential does not identify an active incarnation"
        )
    return _record(row)


@overload
async def terminate_worker_incarnation(
    conn: AsyncConnection,
    incarnation: str,
    authority_kind: Literal["local"],
    binding: LocalAuthorityBinding,
    outcome: TerminationOutcome,
) -> bool: ...


@overload
async def terminate_worker_incarnation(
    conn: AsyncConnection,
    incarnation: str,
    authority_kind: Literal["docker"],
    binding: DockerAuthorityBinding,
    outcome: TerminationOutcome,
) -> bool: ...


@overload
async def terminate_worker_incarnation(
    conn: AsyncConnection,
    incarnation: str,
    authority_kind: Literal["kubernetes"],
    binding: KubernetesAuthorityBinding,
    outcome: TerminationOutcome,
) -> bool: ...


async def terminate_worker_incarnation(
    conn: AsyncConnection,
    incarnation: str,
    authority_kind: AuthorityKind,
    binding: AuthorityBinding,
    outcome: TerminationOutcome,
) -> bool:
    """Terminate or confirm one exact immutable authority-bound incarnation."""
    binding = _validated_binding(authority_kind, binding)
    require_top_level_transaction(conn, "terminate_worker_incarnation")
    async with conn.transaction():
        row = await (
            await conn.execute(
                "SELECT public.terminate_worker_incarnation(%s, %s, %s, %s)",
                (incarnation, authority_kind, Jsonb(binding), outcome),
            )
        ).fetchone()
    assert row is not None
    return bool(row[0])


async def register_kubernetes_worker_incarnation(
    conn: AsyncConnection,
    incarnation: str,
    binding: KubernetesAuthorityBinding,
    credential_hash: bytes,
    credential_envelope: bytes,
    fence_protocol: int,
) -> bool:
    """Persist an exact Kubernetes identity and its encrypted init-only envelope."""
    require_top_level_transaction(conn, "register_kubernetes_worker_incarnation")
    async with conn.transaction():
        row = await (
            await conn.execute(
                "SELECT public.register_kubernetes_worker_incarnation(%s, %s, %s, %s, %s)",
                (
                    incarnation,
                    Jsonb(binding),
                    credential_hash,
                    credential_envelope,
                    fence_protocol,
                ),
            )
        ).fetchone()
    assert row is not None
    return bool(row[0])


async def read_kubernetes_credential_envelope(
    conn: AsyncConnection, incarnation: str, binding: KubernetesAuthorityBinding
) -> bytes | None:
    """Return a pending encrypted envelope only for the exact active Kubernetes binding."""
    require_top_level_transaction(conn, "read_kubernetes_credential_envelope")
    async with conn.transaction():
        row = await (
            await conn.execute(
                "SELECT public.read_kubernetes_credential_envelope(%s, %s)",
                (incarnation, Jsonb(binding)),
            )
        ).fetchone()
    return None if row is None else cast(bytes | None, row[0])


async def acknowledge_kubernetes_credential_envelope(
    conn: AsyncConnection, incarnation: str, binding: KubernetesAuthorityBinding
) -> bool:
    """Durably clear a pending exact envelope, accepting a repeated exact acknowledgment."""
    require_top_level_transaction(conn, "acknowledge_kubernetes_credential_envelope")
    async with conn.transaction():
        row = await (
            await conn.execute(
                "SELECT public.acknowledge_kubernetes_credential_envelope(%s, %s)",
                (incarnation, Jsonb(binding)),
            )
        ).fetchone()
    assert row is not None
    return bool(row[0])
