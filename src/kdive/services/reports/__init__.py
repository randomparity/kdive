"""Report service exports (ADR-0212)."""

from kdive.services.reports.core import (
    DEFAULT_SECTION_CAP,
    Report,
    ReportScope,
    ReportSection,
    Row,
    Section,
    SectionRows,
    Window,
    generate_report,
)

__all__ = [
    "DEFAULT_SECTION_CAP",
    "Report",
    "ReportScope",
    "ReportSection",
    "Row",
    "Section",
    "SectionRows",
    "Window",
    "generate_report",
]
