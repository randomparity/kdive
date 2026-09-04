"""The one place an authority-marked job is diverted from its ordinary handler."""

from __future__ import annotations

from psycopg import AsyncConnection

from kdive.domain.operations.jobs import Job
from kdive.jobs.handlers.external_boot.operations import ExternalBootOperations
from kdive.jobs.handlers.external_boot.ports import EXTERNAL_BOOT_AUTHORITY_MARKER_KEY
from kdive.jobs.models import JobHandler, JobHandlerResult

__all__ = ["route_marked"]


def route_marked(operations: ExternalBootOperations, ordinary: JobHandler) -> JobHandler:
    """Wrap ``ordinary`` so a marked job reaches ``operations`` instead (ADR-0593 decision 3).

    The branch is on **presence** of the marker key, not on validity of the marker — the same rule
    ``kdive.jobs.worker`` already applies when it decodes one, whose comment states "Presence,
    rather than validity, selects the fail-closed path". A payload carrying an undecodable marker
    must not fall through to ``boot_handler`` or ``teardown_handler``, which boot a Run and tear a
    System down under an activation that restricts them.

    There is one router rather than a guard inside each ordinary handler: two independent guards on
    one invariant, where forgetting either runs the wrong operation against a live System, is the
    same behaviour with two places to get it wrong.
    """

    async def handler(conn: AsyncConnection, job: Job) -> JobHandlerResult:
        if EXTERNAL_BOOT_AUTHORITY_MARKER_KEY not in job.payload:
            return await ordinary(conn, job)
        return await operations.run(conn, job)

    return handler
