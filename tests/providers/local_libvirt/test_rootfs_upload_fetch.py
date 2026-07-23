"""Uploaded-rootfs provision-time fetch (ADR-0434)."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from kdive.artifacts.storage import FetchedArtifact, HeadResult
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.rootfs.materialize import (
    RootfsUploadContext,
    upload_rootfs_path,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.rootfs_upload_fetch import (
    fetch_uploaded_rootfs,
)
from kdive.store.objectstore import artifact_key


def _sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


class _FakeStore:
    def __init__(self, data: bytes | None, *, checksum: str | None) -> None:
        self._data = data
        self._checksum = checksum
        self.head_calls = 0
        self.get_calls = 0

    def head(self, key: str) -> HeadResult | None:
        self.head_calls += 1
        if self._data is None:
            return None
        return HeadResult(size_bytes=len(self._data), checksum_sha256=self._checksum, etag="e")

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        self.get_calls += 1
        assert self._data is not None
        return FetchedArtifact(self._data, Sensitivity.SENSITIVE, "rootfs")


def _upload(tmp_path: Path):  # noqa: ANN202 - test helper
    return RootfsUploadContext("local", uuid4(), tmp_path)


def test_fetch_downloads_and_stages_verified_bytes(tmp_path: Path) -> None:
    data = b"rootfs-image-bytes"
    store = _FakeStore(data, checksum=_sha256_b64(data))
    upload = _upload(tmp_path)

    result = fetch_uploaded_rootfs(store, upload)

    assert result == upload_rootfs_path("local", upload.system_id, upload_dir=tmp_path)
    assert result.read_bytes() == data
    assert not result.with_suffix(".qcow2.partial").exists()


def test_fetch_missing_object_is_config_error(tmp_path: Path) -> None:
    store = _FakeStore(None, checksum=None)
    with pytest.raises(CategorizedError) as error:
        fetch_uploaded_rootfs(store, _upload(tmp_path))
    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "never uploaded" in str(error.value)
    assert store.get_calls == 0


def test_fetch_object_without_checksum_is_rejected(tmp_path: Path) -> None:
    store = _FakeStore(b"x", checksum=None)
    with pytest.raises(CategorizedError) as error:
        fetch_uploaded_rootfs(store, _upload(tmp_path))
    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "no stored checksum" in str(error.value)
    assert store.get_calls == 0


def test_fetch_checksum_mismatch_is_infra_error_and_stages_nothing(tmp_path: Path) -> None:
    store = _FakeStore(b"actual-bytes", checksum=_sha256_b64(b"different-bytes"))
    upload = _upload(tmp_path)
    with pytest.raises(CategorizedError) as error:
        fetch_uploaded_rootfs(store, upload)
    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "checksum verification" in str(error.value)
    dest = upload_rootfs_path("local", upload.system_id, upload_dir=tmp_path)
    assert not dest.exists()
    assert not dest.with_suffix(".qcow2.partial").exists()


def test_fetch_reuses_present_file_without_touching_store(tmp_path: Path) -> None:
    upload = _upload(tmp_path)
    dest = upload_rootfs_path("local", upload.system_id, upload_dir=tmp_path)
    dest.write_bytes(b"already-verified")
    store = _FakeStore(b"new", checksum=_sha256_b64(b"new"))

    result = fetch_uploaded_rootfs(store, upload)

    assert result == dest
    assert result.read_bytes() == b"already-verified"
    assert store.head_calls == 0
    assert store.get_calls == 0


def test_fetch_object_key_is_system_rootfs(tmp_path: Path) -> None:
    # Sanity: the fetch resolves the deterministic System-owned rootfs key.
    data = b"z"
    store = _FakeStore(data, checksum=_sha256_b64(data))
    upload = _upload(tmp_path)
    fetch_uploaded_rootfs(store, upload)
    assert artifact_key("local", "systems", str(upload.system_id), "rootfs")
