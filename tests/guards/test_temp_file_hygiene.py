"""Guard: the suite's default temp destination stays inside a directory pytest reclaims (#1613).

A test helper that creates temp state and never removes it leaks one filesystem entry per call,
permanently. The failure is delayed and misattributed: ``/tmp`` runs out of *inodes* long before it
runs out of bytes, so ``df -h`` reports the filesystem as nearly empty while pytest can no longer
create a tmpdir, and a full run reports thousands of collection errors that have nothing to do with
the change under test.

``tests/conftest.py``'s ``session_owned_tempdir`` fixture bounds that class of mistake by repointing
the process default temp root at pytest's base temp directory, which pytest garbage-collects down
to the three most recent runs. These tests assert the *behavior* an unnamed temp destination gets —
not the fixture's existence — so any future change that drops or bypasses the redirection reddens
here rather than silently restoring an unbounded ``/tmp`` leak.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


def _basetemp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.getbasetemp().resolve()


def test_bare_mkdtemp_lands_under_the_pytest_base_temp(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    created = Path(tempfile.mkdtemp()).resolve()
    try:
        assert created.is_relative_to(_basetemp(tmp_path_factory)), (
            f"tempfile.mkdtemp() created {created} outside pytest's base temp directory; "
            "an unnamed temp destination must stay inside the tree pytest reclaims"
        )
    finally:
        shutil.rmtree(created, ignore_errors=True)


def test_undeleted_named_temporary_file_lands_under_the_pytest_base_temp(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    with tempfile.NamedTemporaryFile("w", delete=False) as handle:
        created = Path(handle.name).resolve()
    try:
        assert created.is_relative_to(_basetemp(tmp_path_factory)), (
            f"NamedTemporaryFile(delete=False) created {created} outside pytest's base temp "
            "directory; an unnamed temp destination must stay inside the tree pytest reclaims"
        )
    finally:
        created.unlink(missing_ok=True)


def test_tmpdir_env_matches_the_python_default_for_subprocesses(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    # A subprocess resolves its own temp root from $TMPDIR, not from this process's
    # ``tempfile.tempdir``, so the redirection has to cover both or spawned tools keep
    # writing to the real /tmp.
    tmpdir_env = os.environ.get("TMPDIR")
    assert tmpdir_env is not None, "TMPDIR must be exported for spawned tools"
    assert Path(tmpdir_env).resolve().is_relative_to(_basetemp(tmp_path_factory))
    assert Path(tempfile.gettempdir()).resolve().is_relative_to(_basetemp(tmp_path_factory))
