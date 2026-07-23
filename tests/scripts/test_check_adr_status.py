"""Behavioral tests for scripts/check_adr_status.py."""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.check_adr_status as guard


def _point_guard_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    adr_dir = root / "docs" / "adr"
    monkeypatch.setattr(guard, "_ROOT", root)
    monkeypatch.setattr(guard, "_ADR_DIR", adr_dir)
    monkeypatch.setattr(guard, "_INDEX", adr_dir / "README.md")
    monkeypatch.setattr(guard, "_SRC", root / "src")
    monkeypatch.setattr(guard, "_TESTS", root / "tests")


def _write_repo(
    root: Path,
    *,
    file_status: str = "Accepted",
    index_status: str = "Accepted",
    include_index_row: bool = True,
    source: str = "",
    test_source: str = "",
) -> None:
    adr_dir = root / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (root / "src" / "kdive").mkdir(parents=True)
    (adr_dir / "0001-test-decision.md").write_text(
        f"# Test Decision\n\n- **Status:** {file_status}\n\nDecision body.\n",
        encoding="utf-8",
    )
    row = f"| [0001](0001-test-decision.md) | Test decision | {index_status} |\n"
    (adr_dir / "README.md").write_text(
        "| ADR | Decision | Status |\n| --- | --- | --- |\n" + (row if include_index_row else ""),
        encoding="utf-8",
    )
    (root / "src" / "kdive" / "module.py").write_text(source, encoding="utf-8")
    if test_source:
        (root / "tests").mkdir(parents=True, exist_ok=True)
        (root / "tests" / "test_module.py").write_text(test_source, encoding="utf-8")


def test_clean_adr_status_index_and_uncited_proposed_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(tmp_path, file_status="Proposed", index_status="Proposed")

    assert guard.main() == 0
    assert "index in sync" in capsys.readouterr().out


def test_status_drift_between_file_and_index_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(tmp_path, file_status="Accepted", index_status="Proposed")

    assert guard.main() == 1
    assert "status drift" in capsys.readouterr().out


def test_missing_index_row_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(tmp_path, include_index_row=False)

    assert guard.main() == 1
    assert "missing from the README index" in capsys.readouterr().out


def test_invalid_status_keyword_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(tmp_path, file_status="Maybe", index_status="Maybe")

    assert guard.main() == 1
    assert "invalid Status keyword" in capsys.readouterr().out


def test_proposed_adr_cited_in_source_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(
        tmp_path,
        file_status="Proposed",
        index_status="Proposed",
        source='"""Implements ADR-0001."""\n',
    )

    assert guard.main() == 1
    assert "status is Proposed but it is cited in src/" in capsys.readouterr().out


def test_proposed_adr_cited_only_in_tests_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(
        tmp_path,
        file_status="Proposed",
        index_status="Proposed",
        test_source='"""Guards ADR-0001 (enforced only by this test, no src/ citation)."""\n',
    )

    assert guard.main() == 1
    assert "status is Proposed but it is cited in src/ or tests/" in capsys.readouterr().out


def test_unreadable_source_file_fails_with_path_and_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _point_guard_at(monkeypatch, tmp_path)
    _write_repo(tmp_path, file_status="Proposed", index_status="Proposed")
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
