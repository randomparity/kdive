"""CSV and XLSX rendering of a Report (ADR-0212).

``openpyxl`` is imported here only — the XLSX path is the sole reason for the
dependency. A truncated section carries a header note in both formats so a clipped
spreadsheet is never read as complete.
"""

from __future__ import annotations

import csv
import io

from openpyxl import Workbook

from kdive.services.reports import Report, Section

_TRUNCATED_NOTE = "# truncated: section row cap reached; full data in the spreadsheet"
_SHEET_TITLE_LIMIT = 31


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
    workbook = Workbook()
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
