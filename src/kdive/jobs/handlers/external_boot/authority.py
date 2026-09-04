"""The one authority allocation call this package owns."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import SecretStr

from kdive.domain.operations.jobs import Job
from kdive.jobs.models import ExternalBootAuthorityMarkerV1

__all__ = ["AllocatedAuthority", "allocate_authority"]

_ALLOCATE_SQL = (
    "SELECT status, authority_id, generation, operation_digest "
    "FROM public.allocate_external_boot_authority("
    "sha256(convert_to(%s, 'UTF8')), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


@dataclass(frozen=True, slots=True)
class AllocatedAuthority:
    """The generation-bearing handle a commit must carry back."""

    authority_id: UUID
    generation: int
    operation_digest: str


async def allocate_authority(
    conn: AsyncConnection,
    job: Job,
    marker: ExternalBootAuthorityMarkerV1,
    *,
    incarnation_credential: SecretStr,
) -> AllocatedAuthority | None:
    """Allocate authority for ``job``'s marked operation, or ``None`` when superseded.

    Runs as ``kdive_worker``: ``allocate_external_boot_authority`` raises SQLSTATE ``42501``
    ("worker authority is required") for any other session, and that exception is left to
    propagate unchanged rather than being wrapped — a caller under the wrong role has a
    deployment fault, not an operation failure.

    ``None`` means the function returned ``superseded``. **That does not requeue the job.** No
    authority was allocated, so there is no binding to commit a failure through, and the commit's
    ``fail`` branch is the only path that can set ``jobs.state = 'queued'`` for a marked job. The
    caller's ``terminal`` flag is inert on this path. Reaping such a job is #2203's.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _ALLOCATE_SQL,
            (
                incarnation_credential.get_secret_value(),
                job.id,
                job.attempt,
                marker.activation_id,
                marker.run_id,
                marker.system_id,
                marker.plan_identity,
                marker.purpose,
                marker.provider_kind,
                marker.authority_instance,
                marker.operation_identity,
            ),
        )
        row = await cur.fetchone()
    if row is None or row["status"] != "allocated":
        return None
    return AllocatedAuthority(
        authority_id=row["authority_id"],
        generation=row["generation"],
        operation_digest=row["operation_digest"],
    )
