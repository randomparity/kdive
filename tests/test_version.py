"""Unit tests for the version-info resolver (ADR-0041 decision 5).

Each case mocks one resolution layer at the boundary (the baked import, the `_git`
subprocess wrapper, and `package_version`) and asserts the exact `full_version()` string.
An autouse fixture clears the `version_info` memo between cases so one case's cached
result never masks the next.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kdive import version
from kdive.version import VersionInfo, full_version, version_info


@pytest.fixture(autouse=True)
def _clear_version_cache():
    version_info.cache_clear()
    yield
    version_info.cache_clear()


def _no_baked(monkeypatch):
    monkeypatch.setattr(version, "_from_baked", lambda: None)


def test_baked_release(monkeypatch):
    monkeypatch.setattr(version, "_from_baked", lambda: VersionInfo("0.2.0", "1a2b3c4", True))
    assert full_version() == "0.2.0+g1a2b3c4"


def test_baked_dev(monkeypatch):
    monkeypatch.setattr(version, "_from_baked", lambda: VersionInfo("0.2.0", "1a2b3c4", False))
    assert full_version() == "0.2.0-dev+g1a2b3c4"


def test_live_git_clean_exact_tag_is_release(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")
    calls = {
        ("rev-parse", "--short", "HEAD"): "1a2b3c4",
        ("describe", "--tags", "--exact-match", "HEAD"): "v0.2.0",
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(version, "_git", lambda *a: calls.get(a))
    assert full_version() == "0.2.0+g1a2b3c4"


def test_live_git_on_tag_but_dirty_is_dev(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")
    calls = {
        ("rev-parse", "--short", "HEAD"): "1a2b3c4",
        ("describe", "--tags", "--exact-match", "HEAD"): "v0.2.0",
        ("status", "--porcelain"): " M src/kdive/x.py",
    }
    monkeypatch.setattr(version, "_git", lambda *a: calls.get(a))
    assert full_version() == "0.2.0-dev+g1a2b3c4"


def test_live_git_untagged_is_dev(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")
    calls = {
        ("rev-parse", "--short", "HEAD"): "1a2b3c4",
        ("describe", "--tags", "--exact-match", "HEAD"): None,
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(version, "_git", lambda *a: calls.get(a))
    assert full_version() == "0.2.0-dev+g1a2b3c4"


def test_unknown_no_baked_no_git(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")
    monkeypatch.setattr(version, "_git", lambda *a: None)
    assert full_version() == "0.2.0-dev"


def test_git_returns_none_when_git_executable_is_absent(monkeypatch):
    monkeypatch.setattr(version.shutil, "which", lambda name: None)

    def _unexpected_run(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not be called without git")

    monkeypatch.setattr(version.subprocess, "run", _unexpected_run)

    assert version._git("status", "--porcelain") is None


def test_git_uses_resolved_executable(monkeypatch):
    class _Result:
        stdout = "abc123\n"

    seen: list[list[str]] = []
    monkeypatch.setattr(version.shutil, "which", lambda name: "/usr/bin/git")

    def _run(argv, **_kwargs):
        seen.append(argv)
        return _Result()

    monkeypatch.setattr(version.subprocess, "run", _run)

    assert version._git("rev-parse", "--short", "HEAD") == "abc123"
    assert seen == [
        [
            "/usr/bin/git",
            "-c",
            f"safe.directory={Path(version.__file__).resolve().parents[2]}",
            "-C",
            str(Path(version.__file__).resolve().parents[2]),
            "rev-parse",
            "--short",
            "HEAD",
        ]
    ]


def test_live_git_uses_imported_checkout_across_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    package = checkout / "src/kdive"
    package.mkdir(parents=True)
    (package / "version.py").write_text("# source\n")
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "test",
        ],
        check=True,
    )
    expected = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _no_baked(monkeypatch)
    monkeypatch.setattr(version, "__file__", str(package / "version.py"))
    monkeypatch.chdir(tmp_path)
    assert version_info().commit == expected


def test_live_git_does_not_claim_parent_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = tmp_path / "installed/src/kdive"
    package.mkdir(parents=True)
    (package / "version.py").write_text("# installed\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _no_baked(monkeypatch)
    monkeypatch.setattr(version, "__file__", str(package / "version.py"))
    monkeypatch.chdir(tmp_path)

    assert version_info().commit is None
