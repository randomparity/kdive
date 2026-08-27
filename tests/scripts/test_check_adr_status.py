"""Behavioral tests for scripts/guards/check_adr_status.py."""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.guards.check_adr_status as guard


def _point_guard_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    adr_dir = root / "docs" / "adr"
    monkeypatch.setattr(guard, "_ROOT", root)
    monkeypatch.setattr(guard, "_ADR_DIR", adr_dir)
    monkeypatch.setattr(guard, "_SRC", root / "src")
    monkeypatch.setattr(guard, "_TESTS", root / "tests")


def _write_repo(
    root: Path,
    *,
    file_status: str = "Accepted",
    shape: str = "inline",
    banner_first: bool = False,
    source: str = "",
    test_source: str = "",
) -> None:
    """Write a one-ADR repo for the guard to scan.

    ``shape`` selects which of the two supported status shapes the ADR uses:
    ``inline`` is the pre-0504 bullet (``- **Status:** Accepted``), ``heading`` is the
    ``## Status`` section the records gate requires from ADR-0504 on, and
    ``heading-empty`` is that section with no value under it. ``banner_first`` puts a
    supersession banner above the status line inside the section, which the guard must
    skip rather than read as the status.
    """
    adr_dir = root / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (root / "src" / "kdive").mkdir(parents=True)

    if shape == "inline":
        body = f"# ADR 0001 — Test Decision\n\n- **Status:** {file_status}\n\nDecision body.\n"
    elif shape == "heading":
        banner = "> **Superseded by [0002](0002-later.md)** (2026-07-30)\n"
        under = (banner + f"{file_status}\n") if banner_first else f"{file_status}\n"
        body = f"# 0001 — Test Decision\n\n## Status\n\n{under}\n## Context\n\nBody.\n"
    elif shape == "heading-empty":
        body = "# 0001 — Test Decision\n\n## Status\n\n## Context\n\nBody.\n"
    else:  # pragma: no cover - guarded by the caller
        raise ValueError(f"unknown shape {shape!r}")

    (adr_dir / "0001-test-decision.md").write_text(body, encoding="utf-8")
    (root / "src" / "kdive" / "module.py").write_text(source, encoding="utf-8")
    if test_source:
        (root / "tests").mkdir(parents=True, exist_ok=True)
        (root / "tests" / "test_module.py").write_text(test_source, encoding="utf-8")


def test_clean_status_and_uncited_proposed_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(tmp_path, file_status="Proposed")

    assert guard.main() == 0
    out = capsys.readouterr().out
    assert "no shipped-but-Proposed drift" in out
    # The index-sync invariant is gone with the index itself (ADR-0504); the guard must not
    # claim to have checked it.
    assert "index" not in out


def test_invalid_status_keyword_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(tmp_path, file_status="Maybe")

    assert guard.main() == 1
    assert "invalid Status keyword" in capsys.readouterr().out


def test_status_under_a_heading_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR-0504's shape: `## Status` heading with `Accepted (YYYY-MM-DD)` beneath it."""
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(tmp_path, file_status="Accepted (2026-07-29)", shape="heading")

    assert guard.main() == 0
    assert "1 ADRs" in capsys.readouterr().out


def test_invalid_status_keyword_under_a_heading_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The heading shape is validated, not merely parsed — a bad keyword there still fails."""
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(tmp_path, file_status="Ratified (2026-07-29)", shape="heading")

    assert guard.main() == 1
    assert "invalid Status keyword 'Ratified'" in capsys.readouterr().out


def test_empty_status_section_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `## Status` heading with the next section straight after it carries no status."""
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(tmp_path, shape="heading-empty")

    assert guard.main() == 1
    assert "no Status line found" in capsys.readouterr().out


def test_supersession_banner_does_not_mask_the_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A banner above the status line is skipped, so the real keyword is still validated.

    Without the skip the guard would read `> **Superseded by ...` as the status value and
    report an invalid keyword for a well-formed record.
    """
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(tmp_path, file_status="Ratified (2026-07-29)", shape="heading", banner_first=True)

    assert guard.main() == 1
    # It reached the status line beneath the banner rather than stopping at the banner.
    assert "invalid Status keyword 'Ratified'" in capsys.readouterr().out


def test_proposed_adr_cited_in_source_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(tmp_path, file_status="Proposed", source='"""Implements ADR-0001."""\n')

    assert guard.main() == 1
    assert "status is Proposed but it is cited in src/" in capsys.readouterr().out


def test_proposed_adr_cited_only_in_tests_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(
        tmp_path,
        file_status="Proposed",
        test_source='"""Guards ADR-0001 (enforced only by this test, no src/ citation)."""\n',
    )

    assert guard.main() == 1
    assert "status is Proposed but it is cited in src/ or tests/" in capsys.readouterr().out


def test_unreadable_source_file_fails_with_path_and_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(tmp_path, file_status="Proposed")
    source = tmp_path / "src" / "kdive" / "module.py"
    original_read_text = Path.read_text

    def fail_for_source(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        if path == source:
            raise PermissionError("denied")
        return original_read_text(path, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "read_text", fail_for_source)

    assert guard.main() == 1
    captured = capsys.readouterr()
    assert "src/kdive/module.py" in captured.err
    assert "PermissionError" in captured.err
