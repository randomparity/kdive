"""The env-coverage guard documents every KDIVE_* token (companion to config_env_guard)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_env_documented import (
    _NOT_ENV,
    documented_names,
    find_undocumented,
    main,
)


def test_tree_is_clean() -> None:
    """The committed tree has no undocumented KDIVE_* tokens (the gate CI enforces)."""
    from scripts.check_env_documented import _scan_files

    assert find_undocumented(_scan_files(), documented_names()) == []


def test_flags_an_undocumented_token(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text('import os\nos.getenv("KDIVE_TOTALLY_NEW")\n', encoding="utf-8")

    found = find_undocumented([probe], documented_names())

    assert [u.token for u in found] == ["KDIVE_TOTALLY_NEW"]
    assert found[0].line == 2


def test_a_registry_setting_is_documented(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text('os.getenv("KDIVE_DATABASE_URL")\n', encoding="utf-8")

    assert find_undocumented([probe], documented_names()) == []


def test_an_external_env_var_is_documented(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text('os.getenv("KDIVE_GUEST_IMAGE")\n', encoding="utf-8")

    assert find_undocumented([probe], documented_names()) == []


def test_glob_prefix_fragment_is_not_flagged(tmp_path: Path) -> None:
    # Prose/docstrings reference families as `KDIVE_S3_*`; the trailing-underscore fragment the
    # token regex captures must not be treated as an undocumented variable.
    probe = tmp_path / "probe.py"
    probe.write_text('"""Built from the KDIVE_S3_* environment."""\n', encoding="utf-8")

    assert find_undocumented([probe], documented_names()) == []


def test_not_env_token_is_ignored(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text('NS = "KDIVE_METADATA_NS"\n', encoding="utf-8")

    assert "KDIVE_METADATA_NS" in _NOT_ENV
    assert find_undocumented([probe], documented_names()) == []


def test_unreadable_scanned_file_fails_with_path_and_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = tmp_path / "src" / "probe.py"
    probe.parent.mkdir()
    probe.write_text('os.getenv("KDIVE_DATABASE_URL")\n', encoding="utf-8")
    original_read_text = Path.read_text

    def fail_for_probe(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        if path == probe:
            raise PermissionError("denied")
        return original_read_text(path, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr("scripts.check_env_documented._ROOT", tmp_path)
    monkeypatch.setattr("scripts.check_env_documented._SCAN_DIRS", ("src",))
    monkeypatch.setattr(Path, "read_text", fail_for_probe)

    assert main() == 1
    captured = capsys.readouterr()
    assert "src/probe.py" in captured.err
    assert "PermissionError" in captured.err
