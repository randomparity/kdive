"""CSV and XLSX rendering of a Report (ADR-0212).

``openpyxl`` is imported lazily only for XLSX output. A truncated section carries a
header note in both formats so a clipped spreadsheet is never read as complete.
"""

from __future__ import annotations

import csv
import importlib
import io
from typing import Any, cast

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.services.reports.core import Report, Section

_TRUNCATED_NOTE = "# truncated: section row cap reached; full data in the spreadsheet"
_SHEET_TITLE_LIMIT = 31


def _workbook_class() -> Any:
    """Return openpyxl's Workbook class, or raise the documented dependency error."""
    try:
        module = importlib.import_module("openpyxl")
    except ImportError as exc:
        raise CategorizedError(
            "XLSX report rendering requires the openpyxl runtime dependency",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"dependency": "openpyxl"},
        ) from exc
    return cast(Any, module).Workbook


def _cell(value: object) -> str:
    """Render one value as a cell string; ``None`` becomes an empty cell."""
    return "" if value is None else str(value)


def _section_csv(section: Section) -> bytes:
    buffer = io.StringIO()
    if section.truncated:
        buffer.write(f"{_TRUNCATED_NOTE}\n")
    writer = csv.writer(buffer)
    writer.writerow(section.columns)
    for row in section.rows:
        writer.writerow([_cell(row.get(column)) for column in section.columns])
    return buffer.getvalue().encode("utf-8")


def render_csv(report: Report) -> dict[str, bytes]:
    """Render each section to its own CSV file, keyed by section key."""
    return {section.key: _section_csv(section) for section in report.sections}


def render_xlsx(report: Report) -> bytes:
    """Render the report to one workbook with one sheet per section, in registry order."""
    workbook = _workbook_class()()
    default_sheet = workbook.active
    if default_sheet is not None:
        workbook.remove(default_sheet)
    for section in report.sections:
        sheet = workbook.create_sheet(title=section.key[:_SHEET_TITLE_LIMIT])
        if section.truncated:
            sheet.append([_TRUNCATED_NOTE])
        sheet.append(list(section.columns))
        for row in section.rows:
            sheet.append([_cell(row.get(column)) for column in section.columns])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
