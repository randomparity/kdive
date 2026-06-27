"""Write a report's spreadsheet renderings to the object store (ADR-0212).

``register_artifact_row`` only builds the row — the caller inserts it after the object
write (ADR-0005 write-before-commit). This helper therefore takes ``conn`` and, per
rendered file, does put → build-row → ``ARTIFACTS.insert`` → presign, returning the
``{ref_key: presigned_url}`` map the tool surfaces in ``refs``. A synthetic report
``owner_id`` (no foreign key) means a reconciler GC sweep, not a row teardown, reaps the
object later.
"""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection

from kdive.artifacts.registration import register_artifact_row
from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.db.repositories import ARTIFACTS
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.services.reports.core import Report
from kdive.services.reports.render import render_csv, render_xlsx

_TENANT = "local"
_RETENTION_CLASS = "report"
_OWNER_KIND = "reports"


class ReportArtifactStore(Protocol):
    """The object-store surface the report write/reap paths need."""

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...
    def presign_get(self, key: str, *, expires_in: int) -> str: ...
    def delete(self, key: str) -> None: ...


def _rendered_files(report: Report, formats: tuple[str, ...]) -> list[tuple[str, str, bytes]]:
    """Return ``(ref_key, object_name, data)`` for each requested format."""
    files: list[tuple[str, str, bytes]] = []
    if "csv" in formats:
        for section_key, data in render_csv(report).items():
            files.append((f"csv:{section_key}", f"{section_key}.csv", data))
    if "xlsx" in formats:
        files.append(("xlsx", "report.xlsx", render_xlsx(report)))
    return files


async def write_report_artifacts(
    conn: AsyncConnection,
    report: Report,
    formats: tuple[str, ...],
    *,
    store: ReportArtifactStore,
    report_id: UUID,
    ttl: int,
    tenant: str = _TENANT,
) -> dict[str, str]:
    """Render, store, register, and presign the report's spreadsheets.

    Raises:
        CategorizedError: A store put/presign failed; the caller degrades the response to
            inline-only.
    """
    refs: dict[str, str] = {}
    for ref_key, name, data in _rendered_files(report, formats):
        request = ArtifactWriteRequest(
            tenant=tenant,
            owner_kind=_OWNER_KIND,
            owner_id=str(report_id),
            name=name,
            data=data,
            sensitivity=Sensitivity.REDACTED,
            retention_class=_RETENTION_CLASS,
        )
        stored = await asyncio.to_thread(store.put_artifact, request)
        row = register_artifact_row(stored, owner_kind=_OWNER_KIND, owner_id=report_id)
        async with conn.transaction():
            await ARTIFACTS.insert(conn, row)
        refs[ref_key] = await asyncio.to_thread(store.presign_get, stored.key, expires_in=ttl)
    return refs
