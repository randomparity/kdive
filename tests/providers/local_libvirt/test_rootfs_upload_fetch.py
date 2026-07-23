"""Uploaded-rootfs provision-time fetch (ADR-0434, ADR-0438)."""

from __future__ import annotations

import base64
import gzip
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

# A minimal canonical qcow2 base: the magic followed by arbitrary body bytes.
_QCOW2 = b"QFI\xfb" + b"canonical-qcow2-body"


def _sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


class _FakeStore:
    def __init__(self, data: bytes | None, *, checksum: str | None) -> None:
        self._data = data
        self._checksum = checksum
        self.head_calls = 0
        self.get_calls = 0
        self.range_calls = 0

    def head(self, key: str) -> HeadResult | None:
        self.head_calls += 1
        if self._data is None:
            return None
        return HeadResult(size_bytes=len(self._data), checksum_sha256=self._checksum, etag="e")

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        self.get_calls += 1
        assert self._data is not None
        return FetchedArtifact(self._data, Sensitivity.SENSITIVE, "rootfs")

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        self.range_calls += 1
        assert self._data is not None
        return self._data[start : start + length]


def _upload(tmp_path: Path):  # noqa: ANN202 - test helper
    return RootfsUploadContext("local", uuid4(), tmp_path)


def _dest(upload: RootfsUploadContext, tmp_path: Path) -> Path:
    return upload_rootfs_path("local", upload.system_id, upload_dir=tmp_path)


# --- identity path (unchanged behavior + new magic check) ---------------------------------------


def test_fetch_downloads_and_stages_verified_bytes(tmp_path: Path) -> None:
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    upload = _upload(tmp_path)

    result = fetch_uploaded_rootfs(store, upload)

    assert result == _dest(upload, tmp_path)
    assert result.read_bytes() == _QCOW2
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
    dest = _dest(upload, tmp_path)
    assert not dest.exists()
    assert not dest.with_suffix(".qcow2.partial").exists()


def test_fetch_non_qcow2_identity_is_config_error_and_stages_nothing(tmp_path: Path) -> None:
    data = b"not-a-qcow2-image"  # correct checksum but wrong format
    store = _FakeStore(data, checksum=_sha256_b64(data))
    upload = _upload(tmp_path)
    with pytest.raises(CategorizedError) as error:
        fetch_uploaded_rootfs(store, upload)
    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "qcow2" in str(error.value)
    assert not _dest(upload, tmp_path).exists()


def test_fetch_reuses_present_file_without_touching_store(tmp_path: Path) -> None:
    upload = _upload(tmp_path)
    dest = _dest(upload, tmp_path)
    dest.write_bytes(b"already-verified")
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    result = fetch_uploaded_rootfs(store, upload)

    assert result == dest
    assert result.read_bytes() == b"already-verified"
    assert store.head_calls == 0
    assert store.get_calls == 0


def test_fetch_object_key_is_system_rootfs(tmp_path: Path) -> None:
    # Sanity: the fetch resolves the deterministic System-owned rootfs key.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    upload = _upload(tmp_path)
    fetch_uploaded_rootfs(store, upload)
    assert artifact_key("local", "systems", str(upload.system_id), "rootfs")


# --- gzip transport-strip path (ADR-0438) -------------------------------------------------------


def test_fetch_gzip_streams_decompressed_qcow2(tmp_path: Path) -> None:
    canonical = _QCOW2 + b"x" * 4096
    compressed = gzip.compress(canonical)
    store = _FakeStore(compressed, checksum=_sha256_b64(compressed))
    upload = _upload(tmp_path)

    result = fetch_uploaded_rootfs(store, upload, encoding="gzip", uncompressed_size=len(canonical))

    assert result == _dest(upload, tmp_path)
    assert result.read_bytes() == canonical
    assert not result.with_suffix(".qcow2.partial").exists()
    # Streamed via ranged reads, not a whole-object buffer.
    assert store.get_calls == 0
    assert store.range_calls >= 1


def test_fetch_gzip_bomb_is_rejected_and_stages_nothing(tmp_path: Path) -> None:
    canonical = _QCOW2 + b"y" * 8192
    compressed = gzip.compress(canonical)
    store = _FakeStore(compressed, checksum=_sha256_b64(compressed))
    upload = _upload(tmp_path)

    with pytest.raises(CategorizedError) as error:
        # Declare a smaller uncompressed_size than the real output → bomb guard trips.
        fetch_uploaded_rootfs(store, upload, encoding="gzip", uncompressed_size=64)

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    dest = _dest(upload, tmp_path)
    assert not dest.exists()
    assert not dest.with_suffix(".qcow2.partial").exists()


def test_fetch_gzip_non_qcow2_canonical_is_rejected(tmp_path: Path) -> None:
    canonical = b"decodes-fine-but-not-a-qcow2"
    compressed = gzip.compress(canonical)
    store = _FakeStore(compressed, checksum=_sha256_b64(compressed))
    upload = _upload(tmp_path)

    with pytest.raises(CategorizedError) as error:
        fetch_uploaded_rootfs(store, upload, encoding="gzip", uncompressed_size=len(canonical))

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "qcow2" in str(error.value)
    dest = _dest(upload, tmp_path)
    assert not dest.exists()
    assert not dest.with_suffix(".qcow2.partial").exists()


def test_fetch_gzip_transport_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    canonical = _QCOW2 + b"z" * 128
    compressed = gzip.compress(canonical)
    store = _FakeStore(compressed, checksum=_sha256_b64(b"a-different-object"))
    upload = _upload(tmp_path)

    with pytest.raises(CategorizedError) as error:
        fetch_uploaded_rootfs(store, upload, encoding="gzip", uncompressed_size=len(canonical))

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert not _dest(upload, tmp_path).exists()


def test_fetch_gzip_without_uncompressed_size_is_config_error(tmp_path: Path) -> None:
    compressed = gzip.compress(_QCOW2)
    store = _FakeStore(compressed, checksum=_sha256_b64(compressed))
    upload = _upload(tmp_path)

    with pytest.raises(CategorizedError) as error:
        fetch_uploaded_rootfs(store, upload, encoding="gzip", uncompressed_size=None)

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "uncompressed_size" in str(error.value)


def test_fetch_identity_sentinel_stages_verbatim(tmp_path: Path) -> None:
    # The explicit "identity" sentinel behaves exactly like an absent encoding.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    upload = _upload(tmp_path)

    result = fetch_uploaded_rootfs(store, upload, encoding="identity")

    assert result.read_bytes() == _QCOW2
    assert store.range_calls == 0


def test_fetch_unsupported_encoding_is_config_error(tmp_path: Path) -> None:
    # Defence in depth: a codec the declaration validator would reject is named, not staged as-is.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    upload = _upload(tmp_path)

    with pytest.raises(CategorizedError) as error:
        fetch_uploaded_rootfs(store, upload, encoding="zstd", uncompressed_size=999)

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "unsupported transport encoding" in str(error.value)
    assert store.get_calls == 0
    assert store.range_calls == 0
    assert not _dest(upload, tmp_path).exists()
