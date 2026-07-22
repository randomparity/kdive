"""Direct service tests for report artifact writing (ADR-0212)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.services.reports.artifacts import write_report_artifacts
from kdive.services.reports.core import Report, Section
from kdive.services.reports.render import render_xlsx

_AS_OF = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)


class _RecordingStore:
    """Records put requests and presign calls; mints key-embedding URLs."""

    def __init__(self) -> None:
        self.puts: list[ArtifactWriteRequest] = []
        self.presigns: list[tuple[str, int]] = []

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.puts.append(request)
        return StoredArtifact(
            key=request.key(),
            etag="etag",
            sensitivity=request.sensitivity,
            retention_class=request.retention_class,
        )

    def presign_get(self, key: str, *, expires_in: int) -> str:
        self.presigns.append((key, expires_in))
        return f"https://signed.test/{key}?exp={expires_in}"

    def delete(self, key: str) -> None:  # pragma: no cover - unused here
        pass


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _report() -> Report:
    section = Section(
        key="inventory",
        columns=("system_id", "vcpus"),
        rows=({"system_id": "s1", "vcpus": 4},),
        truncated=False,
    )
    return Report(sections=(section,), as_of=_AS_OF)


def test_write_report_artifacts_xlsx_puts_named_data_and_presigns(migrated_url: str) -> None:
    report = _report()
    report_id = uuid4()
    store = _RecordingStore()
    expected_xlsx = render_xlsx(report)

    async def _run() -> dict[str, str]:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            return await write_report_artifacts(
                conn, report, ("xlsx",), store=store, report_id=report_id, ttl=900
            )

    refs = asyncio.run(_run())

    # One xlsx put carrying the rendered bytes, the fixed object name, and the report owner_id.
    assert len(store.puts) == 1
    put = store.puts[0]
    assert put.name == "report.xlsx"
    assert put.data == expected_xlsx
    assert put.owner_id == str(report_id)
    # The presign uses the stored key and the requested ttl, and surfaces under the xlsx ref.
    stored_key = put.key()
    assert store.presigns == [(stored_key, 900)]
    assert refs["xlsx"] == f"https://signed.test/{stored_key}?exp=900"
