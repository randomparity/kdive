"""A registered secret is scrubbed from report rows and rendered artifacts (ADR-0208)."""

from __future__ import annotations

from datetime import UTC, datetime

from kdive.mcp.tools.reports.generate import _normalized_report
from kdive.security.secrets.redaction import REDACTION, Redactor
from kdive.services.reports import Report, Section
from kdive.services.reports.render import render_csv, render_xlsx

_AS_OF = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
_SECRET = "sup3r-s3cret-token-value"


def _report_with_secret() -> Report:
    section = Section(
        key="inventory",
        columns=("system_id", "note"),
        rows=({"system_id": "s1", "note": f"prefix {_SECRET} suffix"},),
        truncated=False,
    )
    return Report(sections=(section,), as_of=_AS_OF)


def test_normalized_report_redacts_registered_secret() -> None:
    redactor = Redactor(secret_values=[_SECRET])
    redacted = _normalized_report(_report_with_secret(), redactor)
    note = redacted.sections[0].rows[0]["note"]
    assert isinstance(note, str)
    assert _SECRET not in note
    assert REDACTION in note


def test_secret_absent_from_rendered_artifacts() -> None:
    redactor = Redactor(secret_values=[_SECRET])
    redacted = _normalized_report(_report_with_secret(), redactor)
    assert _SECRET.encode() not in render_csv(redacted)["inventory"]
    assert _SECRET.encode() not in render_xlsx(redacted)
