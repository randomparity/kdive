"""Pure-surface tests for the profile-kind residue sweep (issue #1907, ADR-0579).

No database access here — see :mod:`kdive.admin.profile_kinds`'s docstring for the module
split. Database-backed tests for :func:`scan_profile_kinds` land in a later task.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from kdive.admin.profile_kinds import (
    ProfileKindMismatch,
    _section_label,
    format_profile_kind_result,
)

_REDACTED_URL = "postgresql://demo:***@db.example/kdive"

# 500 keys outside `_KNOWN_KINDS`, all mapping to `<unrecognized>` and deduplicating to one entry.
_FIVE_HUNDRED_UNKNOWN_KEYS = {f"key-{i}": {} for i in range(500)}


_DEFAULT_SYSTEM_ID = UUID("00000000-0000-0000-0000-000000000001")


def _make_mismatch(
    *,
    project: str = "demo",
    system_id: UUID = _DEFAULT_SYSTEM_ID,
    state: str = "ready",
    profile_section: str = "fault-inject",
    resource_kind: str = "fault-inject",
) -> ProfileKindMismatch:
    return ProfileKindMismatch(
        system_id=system_id,
        project=project,
        state=state,
        profile_section=profile_section,
        resource_kind=resource_kind,
    )


def _mismatch_line(report: str) -> str:
    """Return the sole `system=...` row from a formatted report."""
    lines = [line for line in report.splitlines() if line.startswith("system=")]
    assert len(lines) == 1
    return lines[0]


# --- _section_label ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        pytest.param({"local-libvirt": {"a": 1}}, "local-libvirt", id="one-key-object"),
        pytest.param(
            {"fault-inject": {}, "local-libvirt": {}},
            "fault-inject,local-libvirt",
            id="two-sections",
        ),
        pytest.param(_FIVE_HUNDRED_UNKNOWN_KEYS, "<unrecognized>", id="500-unknown-keys"),
        pytest.param(
            {"fault-inject": None}, "fault-inject=<not-an-object>", id="section-not-object-null"
        ),
        pytest.param(
            {"fault-inject": []}, "fault-inject=<not-an-object>", id="section-not-object-array"
        ),
        pytest.param(
            {"fault-inject": "x"}, "fault-inject=<not-an-object>", id="section-not-object-string"
        ),
        pytest.param({"\x1b[31mBOOM": {}}, "<unrecognized>", id="unrecognized-key"),
        pytest.param({}, "<none>", id="empty-object"),
        pytest.param(None, "<none>", id="none-json-null"),
        pytest.param(None, "<none>", id="none-absent-provider"),
        pytest.param("nope", "<not-an-object>", id="string-scalar"),
        pytest.param([1, 2], "<not-an-object>", id="array"),
        pytest.param(7, "<not-an-object>", id="number"),
        pytest.param(True, "<not-an-object>", id="bool"),
    ],
)
def test_section_label_measured_table(provider: object, expected: str) -> None:
    assert _section_label(provider) == expected


# --- format_profile_kind_result: charset + tokenization (spec Testing item 8) --------------


@pytest.mark.parametrize(
    "hostile",
    ["\x1b", "\x7f", "\x9b", "\xa0", "\u202e"],
    ids=["esc", "del", "csi", "nbsp", "rlo"],
)
def test_format_charset_whole_line_is_printable(hostile: str) -> None:
    mismatch = _make_mismatch(project=f"proj-{hostile}-name")
    report = format_profile_kind_result([mismatch], redacted_url=_REDACTED_URL)

    line = _mismatch_line(report)

    assert line.isprintable()


@pytest.mark.parametrize(
    "project",
    [
        "x state=ready profile_section=fault-inject",
        'x" state=ready profile_section=fault-inject resource_kind=fault-inject junk="y',
    ],
    ids=["no-quote", "forged-double-quote"],
)
def test_format_tokenization_project_cannot_forge_a_field(project: str) -> None:
    mismatch = _make_mismatch(project=project)
    report = format_profile_kind_result([mismatch], redacted_url=_REDACTED_URL)
    line = _mismatch_line(report)

    anchor = f"project={project!r}"
    assert anchor in line  # fail here, not on an IndexError below
    tail = line.split(anchor, 1)[1]

    # Documentation, not the bite: once the anchor matches, the remainder of the line is the
    # fixed suffix, so these counts are a constant for every input — keep them as an
    # executable description of the tail's shape, not as the property that catches forgery.
    assert tail.count("state=") == 1
    assert tail.count("profile_section=") == 1
    assert tail.count("resource_kind=") == 1


# --- format_profile_kind_result: empty / populated report shape ----------------------------


def test_format_empty_names_redacted_url() -> None:
    report = format_profile_kind_result([], redacted_url=_REDACTED_URL)

    assert report == (
        "verified no System's provisioning-profile provider section mismatches its "
        f"Resource kind in {_REDACTED_URL}"
    )


def test_format_mismatches_header_body_and_closing_block() -> None:
    first = _make_mismatch(
        system_id=UUID("00000000-0000-0000-0000-000000000001"),
        project="proj-a",
        state="ready",
        profile_section="fault-inject",
        resource_kind="fault-inject",
    )
    second = _make_mismatch(
        system_id=UUID("00000000-0000-0000-0000-000000000002"),
        project="proj-b",
        state="provisioning",
        profile_section="<none>",
        resource_kind="local-libvirt",
    )

    report = format_profile_kind_result([first, second], redacted_url=_REDACTED_URL)
    lines = report.splitlines()

    assert lines[0] == (
        "found 2 System(s) whose provisioning-profile provider section does not match "
        f"their Resource kind in {_REDACTED_URL}"
    )
    assert (
        "system=00000000-0000-0000-0000-000000000001 project='proj-a' state=ready "
        "profile_section=fault-inject resource_kind=fault-inject" in report
    )
    assert (
        "system=00000000-0000-0000-0000-000000000002 project='proj-b' state=provisioning "
        "profile_section=<none> resource_kind=local-libvirt" in report
    )
    assert "ADR-0549" in report
    assert "remediation is not automated" in report.lower()
    assert "src/kdive/mcp/tools/lifecycle/control/registrar.py:207" in report
    assert "src/kdive/services/runs/steps.py:445" in report
    assert "src/kdive/jobs/handlers/runs/boot_evidence.py:243" in report
    assert "src/kdive/mcp/tools/lifecycle/vmcore/handlers.py:181" in report
