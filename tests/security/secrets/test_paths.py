"""Tests for the scoped path-safety primitive (ADR-0027 §4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.security.secrets.paths import PathSafetyError, confine_to_root


def test_file_under_root_resolves(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    target.write_text("x")
    assert confine_to_root(target, allowed_root=tmp_path) == target.resolve()


def test_relative_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    escape = root / ".." / "outside"
    with pytest.raises(PathSafetyError):
        confine_to_root(escape, allowed_root=root)


def test_absolute_path_outside_root_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "other" / "secret"
    with pytest.raises(PathSafetyError):
        confine_to_root(outside, allowed_root=root)


def test_symlink_to_existing_outside_file_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("leak")
    link = root / "link"
    link.symlink_to(outside)
    with pytest.raises(PathSafetyError):
        confine_to_root(link, allowed_root=root)


def test_symlink_to_nonexistent_outside_path_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    link = root / "dangling"
    link.symlink_to(tmp_path / "nowhere" / "ghost")
    with pytest.raises(PathSafetyError):
        confine_to_root(link, allowed_root=root)


def test_shell_metachar_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathSafetyError) as exc:
        confine_to_root(tmp_path / "a;b", allowed_root=tmp_path)
    assert str(exc.value) == "secret file reference contains unsafe characters"


def test_control_char_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathSafetyError) as exc:
        confine_to_root(Path(f"{tmp_path}/a\x01b"), allowed_root=tmp_path)
    assert str(exc.value) == "secret file reference contains unsafe characters"


def test_space_in_path_is_admitted(tmp_path: Path) -> None:
    # A space (ord 32) is a legal, common filesystem character: the control-character
    # guard rejects ord < 32, so a space must pass through and resolve under the root.
    target = tmp_path / "my secret.txt"
    target.write_text("x")
    assert confine_to_root(target, allowed_root=tmp_path) == target.resolve()


def test_escape_error_message_names_the_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "other" / "secret"
    with pytest.raises(PathSafetyError) as exc:
        confine_to_root(outside, allowed_root=root)
    assert str(exc.value) == f"secret file reference escapes the allowed root: {outside!r}"


def test_not_yet_existing_tail_under_root_admitted(tmp_path: Path) -> None:
    candidate = tmp_path / "subdir" / "future.txt"
    resolved = confine_to_root(candidate, allowed_root=tmp_path)
    assert resolved == candidate.resolve()
