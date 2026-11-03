"""The remote authority stages the plan's vmlinux beside the module archive (#3131)."""

from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.external_boot import ExternalBootPlan
from kdive.providers.remote_libvirt.debuginfo_module_tree import (
    STAGED_NAME,
    DebuginfoModuleTreeStager,
    installed_tree_identity,
)
from kdive.store.objectstore import ObjectStore
from tests.providers.ports.test_external_boot import _plan_data

_VMLINUX = b"dwarf vmlinux bytes " * 64
_SHA = "sha256:" + hashlib.sha256(_VMLINUX).hexdigest()
_STAGED_MANIFEST = "sha256:" + "7" * 64
_INNER_EVIDENCE: dict[str, object] = {
    "module_source_manifest": "sha256:" + "0" * 64,
    "release": "6",
}
_INNER_INSTALLED = "sha256:" + "1" * 64


class _Inner:
    def __init__(self) -> None:
        self.calls = 0

    def materialize_artifacts(
        self, plan: ExternalBootPlan, directory_fd: int
    ) -> tuple[dict[str, object], str]:
        self.calls += 1
        return dict(_INNER_EVIDENCE), _INNER_INSTALLED


class _Store:
    def __init__(self, data: bytes | None = _VMLINUX) -> None:
        self._data = data
        self.requests: list[tuple[str, str | None]] = []

    @contextmanager
    def get_artifact_stream(
        self, key: str, etag: str | None, *, version_id: str | None = None
    ) -> Iterator[SimpleNamespace]:
        self.requests.append((key, version_id))
        if self._data is None:
            raise CategorizedError("object version is gone", category=ErrorCategory.NOT_FOUND)
        yield SimpleNamespace(reader=io.BytesIO(self._data))


def _plan(*, staged: bool = True, sha: str = _SHA, size: int = len(_VMLINUX)) -> ExternalBootPlan:
    member: dict[str, object] = {"key": "b/vmlinux", "version": "v9", "sha256": sha}
    member["size_bytes"] = size
    if staged:
        member["staged_module_source_manifest"] = _STAGED_MANIFEST
    return ExternalBootPlan.model_validate(_plan_data() | {"debuginfo": member})


def _stage(
    tmp_path: Path, plan: ExternalBootPlan, store: _Store, *, fetch: bool = True
) -> tuple[dict[str, object], str]:
    stager = DebuginfoModuleTreeStager(_Inner(), cast(ObjectStore, store), fetch=fetch)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return stager.materialize_artifacts(plan, descriptor)
    finally:
        os.close(descriptor)


def test_a_staged_plan_gets_the_verified_file_and_the_combined_identities(tmp_path: Path) -> None:
    store = _Store()
    plan = _plan()

    evidence, installed = _stage(tmp_path, plan, store)

    assert (tmp_path / STAGED_NAME).read_bytes() == _VMLINUX
    assert os.listdir(tmp_path) == [STAGED_NAME]
    assert store.requests == [("b/vmlinux", "v9")]
    assert evidence["module_source_manifest"] == _STAGED_MANIFEST
    assert evidence["release"] == "6"
    assert plan.debuginfo is not None
    assert installed == installed_tree_identity(_INNER_INSTALLED, plan.debuginfo)
    assert installed != _INNER_INSTALLED


def test_a_plan_without_the_staged_manifest_passes_through(tmp_path: Path) -> None:
    store = _Store()

    evidence, installed = _stage(tmp_path, _plan(staged=False), store)

    assert (evidence, installed) == (_INNER_EVIDENCE, _INNER_INSTALLED)
    assert os.listdir(tmp_path) == [] and store.requests == []


def test_materialization_identities_need_no_fetch(tmp_path: Path) -> None:
    store = _Store()

    evidence, _installed = _stage(tmp_path, _plan(), store, fetch=False)

    assert evidence["module_source_manifest"] == _STAGED_MANIFEST
    assert os.listdir(tmp_path) == [] and store.requests == []


@pytest.mark.parametrize(
    ("sha", "size"),
    [("sha256:" + "a" * 64, len(_VMLINUX)), (_SHA, len(_VMLINUX) + 1), (_SHA, len(_VMLINUX) - 1)],
)
def test_a_mismatching_object_is_refused_and_leaves_nothing(
    tmp_path: Path, sha: str, size: int
) -> None:
    with pytest.raises(ValueError, match="vmlinux version"):
        _stage(tmp_path, _plan(sha=sha, size=size), _Store())

    assert os.listdir(tmp_path) == []


def test_a_missing_object_version_surfaces_the_store_error(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError, match="gone"):
        _stage(tmp_path, _plan(), _Store(None))

    assert os.listdir(tmp_path) == []


def test_an_existing_matching_file_is_reused_without_a_fetch(tmp_path: Path) -> None:
    (tmp_path / STAGED_NAME).write_bytes(_VMLINUX)
    store = _Store()

    _stage(tmp_path, _plan(), store)

    assert store.requests == []


def test_an_existing_mismatching_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / STAGED_NAME).write_bytes(b"other bytes")

    with pytest.raises(ValueError, match="staged vmlinux"):
        _stage(tmp_path, _plan(), _Store())
