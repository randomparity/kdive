"""generate_report runs each registered section against one shared as_of (ADR-0208)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

from psycopg import AsyncConnection

from kdive.services.reports.core import (
    Report,
    ReportScope,
    ReportSection,
    SectionRows,
    Window,
    generate_report,
)

_AS_OF = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
_NO_CONN = cast(AsyncConnection, None)


class _FakeSection:
    key: str = "fake"
    columns: tuple[str, ...] = ("a", "b")

    async def gather(
        self,
        conn: AsyncConnection,
        scope: ReportScope,
        window: Window,
        as_of: datetime,
        *,
        cap: int,
    ) -> SectionRows:
        assert as_of == _AS_OF
        return SectionRows(rows=({"a": "1", "b": scope.projects[0]},), truncated=False)


def test_generate_report_runs_each_section_with_shared_as_of() -> None:
    async def _run() -> None:
        scope = ReportScope(projects=("proj",), all_projects=False)
        sections: tuple[ReportSection, ...] = (_FakeSection(),)
        report = await generate_report(_NO_CONN, scope, None, _AS_OF, sections=sections)
        assert isinstance(report, Report)
        assert report.as_of == _AS_OF
        assert len(report.sections) == 1
        section = report.sections[0]
        assert section.key == "fake"
        assert section.columns == ("a", "b")
        assert section.rows == ({"a": "1", "b": "proj"},)
        assert section.truncated is False

    asyncio.run(_run())


class _RecordingSection:
    key: str = "rec"
    columns: tuple[str, ...] = ("a",)

    def __init__(self) -> None:
        self.received: tuple[object, Window, int] | None = None

    async def gather(
        self,
        conn: AsyncConnection,
        scope: ReportScope,
        window: Window,
        as_of: datetime,
        *,
        cap: int,
    ) -> SectionRows:
        self.received = (conn, window, cap)
        return SectionRows(rows=(), truncated=False)


def test_generate_report_forwards_conn_window_and_cap() -> None:
    async def _run() -> None:
        sentinel_conn = cast(AsyncConnection, object())
        window: Window = (
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2026, 6, 22, tzinfo=UTC),
        )
        scope = ReportScope(projects=("proj",), all_projects=False)
        section = _RecordingSection()
        await generate_report(sentinel_conn, scope, window, _AS_OF, sections=(section,), cap=7)
        # conn, window, and the explicit cap are forwarded verbatim to each section.
        assert section.received == (sentinel_conn, window, 7)

    asyncio.run(_run())


def test_generate_report_preserves_registry_order() -> None:
    async def _run() -> None:
        class _A(_FakeSection):
            key: str = "a"

        class _B(_FakeSection):
            key: str = "b"

        scope = ReportScope(projects=("proj",), all_projects=False)
        sections: tuple[ReportSection, ...] = (_A(), _B())
        report = await generate_report(_NO_CONN, scope, None, _AS_OF, sections=sections)
        assert [s.key for s in report.sections] == ["a", "b"]

    asyncio.run(_run())
