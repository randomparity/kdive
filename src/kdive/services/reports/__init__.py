"""Report domain: section registry and the composed point-in-time report (ADR-0212).

A report is a fixed, ordered set of :class:`ReportSection`s. Each section gathers its
rows from existing data-access against one shared ``as_of`` snapshot, so every section
of a single report observes the same instant. :func:`generate_report` runs the registry
in order; rendering and the MCP envelope live in sibling modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from psycopg import AsyncConnection

DEFAULT_SECTION_CAP = 500

Window = tuple[datetime | None, datetime | None] | None
Row = dict[str, object]


@dataclass(frozen=True, slots=True)
class ReportScope:
    """The already-authorized project set a report covers.

    ``all_projects`` is the operator (platform-auditor) form; ``projects`` still carries
    the resolved universe so cost rollups and scope predicates have a concrete set.
    """

    projects: tuple[str, ...]
    all_projects: bool


@dataclass(frozen=True, slots=True)
class SectionRows:
    """A section's gathered rows plus whether the per-section cap truncated them."""

    rows: tuple[Row, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class Section:
    """One gathered report section: its schema, rows, and truncation flag."""

    key: str
    columns: tuple[str, ...]
    rows: tuple[Row, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class Report:
    """The composed point-in-time report: ordered sections and their shared ``as_of``."""

    sections: tuple[Section, ...]
    as_of: datetime


class ReportSection(Protocol):
    """A report section: a stable key, an ordered column schema, and a gather coroutine."""

    key: str
    columns: tuple[str, ...]

    async def gather(
        self,
        conn: AsyncConnection,
        scope: ReportScope,
        window: Window,
        as_of: datetime,
        *,
        cap: int,
    ) -> SectionRows: ...


async def generate_report(
    conn: AsyncConnection,
    scope: ReportScope,
    window: Window,
    as_of: datetime,
    *,
    sections: tuple[ReportSection, ...],
    cap: int = DEFAULT_SECTION_CAP,
) -> Report:
    """Gather every section against one shared ``as_of`` into a :class:`Report`.

    Args:
        conn: Async connection; sections read through it (no transaction opened here).
        scope: The already-authorized project set.
        window: Half-open ``(start, end)`` bound for the time-sensitive sections, or ``None``.
        as_of: The single point-in-time snapshot every section observes.
        sections: The ordered section registry to run.
        cap: Per-section row cap.

    Returns:
        A :class:`Report` carrying one :class:`Section` per registered section, in
        registry order.
    """
    gathered: list[Section] = []
    for section in sections:
        result = await section.gather(conn, scope, window, as_of, cap=cap)
        gathered.append(
            Section(
                key=section.key,
                columns=section.columns,
                rows=result.rows,
                truncated=result.truncated,
            )
        )
    return Report(sections=tuple(gathered), as_of=as_of)
