"""Behavior and edge tests for the object-store client (ADR-0017).

The MinIO-backed tests use the session ``minio_store`` fixture and gate on Docker
exactly as the db tests do; the pure tests (key validation, etag normalization,
``register_artifact_row``, env config) run without a container.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from datetime import UTC
from pathlib import Path
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

from kdive.artifacts.registration import register_artifact_row
from kdive.artifacts.storage import (
    ArtifactStreamRequest,
    ArtifactWriteRequest,
    ObjectVersion,
    StoredArtifact,
    VersionBatch,
    VersionPage,
)
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.store.objectstore import (
    ObjectStore,
    _infrastructure_error,
    _local_stream_error,
    _normalize_etag,
    object_store_from_env,
)
from tests.clock import STORE_MTIME

_UNSUPPORTED_VERSIONING_CODES = frozenset(
    {"MethodNotAllowed", "NotImplemented", "UnsupportedOperation"}
)


def _versioning_suspension_is_unsupported(error: ClientError) -> bool:
    code = error.response.get("Error", {}).get("Code")
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in _UNSUPPORTED_VERSIONING_CODES or status in {405, 501}


@contextmanager
def _suspend_versioning_or_skip(store: ObjectStore) -> Iterator[None]:
    """Suspend fixture versioning only around a legacy-identity proof, then always re-enable it."""
    try:
        store._client.put_bucket_versioning(
            Bucket=store._bucket, VersioningConfiguration={"Status": "Suspended"}
        )
    except ClientError as error:
        if _versioning_suspension_is_unsupported(error):
            code = error.response.get("Error", {}).get("Code", "unknown")
            pytest.skip(f"object-store endpoint does not support versioning suspension: {code}")
        raise
    try:
        yield
    finally:
        store._client.put_bucket_versioning(
            Bucket=store._bucket, VersioningConfiguration={"Status": "Enabled"}
        )


def _legacy_inventory_ids_or_skip(store: ObjectStore, key: str) -> tuple[str, ...]:
    """Return suspended-write identities, or skip when inventory lacks them."""
    try:
        page = store.list_version_page(key)
    except CategorizedError as error:
        if error.details.get("s3_error_code") in _UNSUPPORTED_VERSIONING_CODES:
            pytest.skip("object-store endpoint does not support version inventory")
        raise
    version_ids = tuple(entry.version_id for entry in page.entries if entry.key == key)
    if not version_ids:
        pytest.skip("object-store version inventory does not expose a suspended legacy identity")
    return version_ids


def test_normalize_etag_strips_surrounding_quotes() -> None:
    assert _normalize_etag('"abc123"') == "abc123"
    assert _normalize_etag("abc123") == "abc123"
    # Only the surrounding double-quotes are stripped; other edge characters are preserved.
    assert _normalize_etag('"Xabc-9X"') == "Xabc-9X"


class _VersionClient:
    """Canned version-list replies with raw request recording."""

    def __init__(self, pages: list[dict[str, object]]) -> None:
        self._pages = iter(pages)
        self.list_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []

    def list_object_versions(self, **kwargs: object) -> dict[str, object]:
        self.list_calls.append(kwargs)
        return next(self._pages)

    def delete_object(self, **kwargs: object) -> dict[str, object]:
        self.delete_calls.append(kwargs)
        return {}


def _version(
    key: str,
    version_id: str,
    *,
    latest: bool,
    etag: str = '"etag"',
) -> dict[str, object]:
    return {
        "Key": key,
        "VersionId": version_id,
        "LastModified": STORE_MTIME,
        "ETag": etag,
        "IsLatest": latest,
    }


def _marker(key: str, version_id: str, *, latest: bool) -> dict[str, object]:
    return {
        "Key": key,
        "VersionId": version_id,
        "LastModified": STORE_MTIME,
        "IsLatest": latest,
    }


def test_version_values_are_immutable() -> None:
    entry = ObjectVersion("p/key", "null", STORE_MTIME, None, True, True)
    page = VersionPage((entry,), False, None, None)
    batch = VersionBatch("p/key", page.entries, True)

    with pytest.raises(FrozenInstanceError):
        entry.version_id = "changed"  # ty: ignore[invalid-assignment]
    with pytest.raises(FrozenInstanceError):
        page.is_truncated = True  # ty: ignore[invalid-assignment]
    with pytest.raises(FrozenInstanceError):
        batch.history_complete = False  # ty: ignore[invalid-assignment]


def test_list_version_page_lists_data_and_markers_with_continuation() -> None:
    client = _VersionClient(
        [
            {
                "Versions": [_version("p/b", "v2", latest=False)],
                "DeleteMarkers": [_marker("p/a", "m1", latest=True)],
                "IsTruncated": True,
                "NextKeyMarker": "p/b",
                "NextVersionIdMarker": "v2",
            }
        ]
    )

    page = ObjectStore(client, "bucket").list_version_page(
        "p/", key_marker="p/a", version_id_marker="v1", max_keys=17
    )

    assert client.list_calls == [
        {
            "Bucket": "bucket",
            "Prefix": "p/",
            "KeyMarker": "p/a",
            "VersionIdMarker": "v1",
            "MaxKeys": 17,
        }
    ]
    assert page.entries == (
        ObjectVersion("p/a", "m1", STORE_MTIME, None, True, True),
        ObjectVersion("p/b", "v2", STORE_MTIME, "etag", False, False),
    )
    assert (page.is_truncated, page.next_key_marker, page.next_version_id_marker) == (
        True,
        "p/b",
        "v2",
    )


def test_iter_prefix_version_pages_resumes_with_returned_markers() -> None:
    client = _VersionClient(
        [
            {
                "Versions": [_version("p/key", "v1", latest=True)],
                "IsTruncated": True,
                "NextKeyMarker": "p/key",
                "NextVersionIdMarker": "v1",
            },
            {"DeleteMarkers": [_marker("p/key", "m2", latest=True)], "IsTruncated": False},
        ]
    )

    pages = list(ObjectStore(client, "bucket").iter_prefix_version_pages("p/"))

    assert [page.entries[0].version_id for page in pages] == ["v1", "m2"]
    assert client.list_calls == [
        {"Bucket": "bucket", "Prefix": "p/", "MaxKeys": 1000},
        {
            "Bucket": "bucket",
            "Prefix": "p/",
            "KeyMarker": "p/key",
            "VersionIdMarker": "v1",
            "MaxKeys": 1000,
        },
    ]


def test_iter_prefix_version_pages_supports_key_only_resume() -> None:
    client = _VersionClient([{"IsTruncated": False}])

    assert list(ObjectStore(client, "bucket").iter_prefix_version_pages("p/", key_marker="p/key"))
    assert client.list_calls == [
        {"Bucket": "bucket", "Prefix": "p/", "KeyMarker": "p/key", "MaxKeys": 1000}
    ]


@pytest.mark.parametrize("max_keys", [0, 1001, True])
def test_list_version_page_rejects_out_of_range_limits(max_keys: int) -> None:
    with pytest.raises(ValueError):
        ObjectStore(_VersionClient([]), "bucket").list_version_page("p/", max_keys=max_keys)


def test_truncated_version_page_requires_advancing_markers() -> None:
    client = _VersionClient(
        [
            {
                "Versions": [],
                "IsTruncated": True,
                "NextKeyMarker": "p/key",
                "NextVersionIdMarker": "v1",
            }
        ]
    )

    with pytest.raises(CategorizedError) as excinfo:
        list(
            ObjectStore(client, "bucket").iter_prefix_version_pages(
                "p/", key_marker="p/key", version_id_marker="v1"
            )
        )

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_capture_exact_versions_excludes_sibling_keys_and_accepts_null() -> None:
    client = _VersionClient(
        [
            {
                "Versions": [
                    _version("p/key", "null", latest=False),
                    _version("p/key-extra", "sibling", latest=True),
                    _version("p/key", "v2", latest=True),
                ],
                "IsTruncated": False,
            }
        ]
    )

    batch = ObjectStore(client, "bucket").capture_exact_versions("p/key", 2)

    assert batch == VersionBatch(
        "p/key",
        (
            ObjectVersion("p/key", "null", STORE_MTIME, "etag", False, False),
            ObjectVersion("p/key", "v2", STORE_MTIME, "etag", True, False),
        ),
        True,
    )


def test_capture_exact_versions_keeps_latest_when_history_is_incomplete() -> None:
    client = _VersionClient(
        [
            {
                "Versions": [_version("p/key", "v1", latest=False)],
                "IsTruncated": True,
                "NextKeyMarker": "p/key",
                "NextVersionIdMarker": "v1",
            }
        ]
    )

    batch = ObjectStore(client, "bucket").capture_exact_versions("p/key", 1)

    assert batch.history_complete is False
    assert batch.targets[0].version_id == "v1"


def test_capture_exact_versions_keeps_the_latest_entry_inside_the_bound() -> None:
    client = _VersionClient(
        [
            {
                "Versions": [
                    _version("p/key", "a-old", latest=False),
                    _version("p/key", "z-latest", latest=True),
                ],
                "IsTruncated": False,
            }
        ]
    )

    batch = ObjectStore(client, "bucket").capture_exact_versions("p/key", 1)

    assert batch.targets[0].version_id == "z-latest"
    assert batch.history_complete is False


@pytest.mark.parametrize("limit", [0, -1, True])
def test_capture_exact_versions_requires_a_positive_limit(limit: int) -> None:
    with pytest.raises(ValueError):
        ObjectStore(_VersionClient([]), "bucket").capture_exact_versions("p/key", limit)


def test_delete_version_always_names_identity() -> None:
    client = _VersionClient([])

    ObjectStore(client, "bucket").delete_version("p/key", "v2")

    assert client.delete_calls == [{"Bucket": "bucket", "Key": "p/key", "VersionId": "v2"}]


def test_delete_batch_deletes_nonlatest_before_latest() -> None:
    client = _VersionClient([])
    batch = VersionBatch(
        "p/key",
        (
            ObjectVersion("p/key", "v1", STORE_MTIME, "etag", False, False),
            ObjectVersion("p/key", "v2", STORE_MTIME, "etag", True, False),
        ),
        True,
    )

    assert ObjectStore(client, "bucket").delete_batch(batch) is True
    assert [call["VersionId"] for call in client.delete_calls] == ["v1", "v2"]


def test_delete_batch_retains_latest_when_capture_is_incomplete() -> None:
    client = _VersionClient([])
    batch = VersionBatch(
        "p/key",
        (
            ObjectVersion("p/key", "v1", STORE_MTIME, "etag", False, False),
            ObjectVersion("p/key", "v2", STORE_MTIME, "etag", True, False),
        ),
        False,
    )

    assert ObjectStore(client, "bucket").delete_batch(batch) is False
    assert [call["VersionId"] for call in client.delete_calls] == ["v1"]


def test_delete_batch_does_not_delete_latest_after_a_nonlatest_failure() -> None:
    class _FailingVersionClient(_VersionClient):
        def delete_object(self, **kwargs: object) -> dict[str, object]:
            super().delete_object(**kwargs)
            raise EndpointConnectionError(endpoint_url="http://unreachable")

    client = _FailingVersionClient([])
    batch = VersionBatch(
        "p/key",
        (
            ObjectVersion("p/key", "v1", STORE_MTIME, "etag", False, False),
            ObjectVersion("p/key", "v2", STORE_MTIME, "etag", True, False),
        ),
        True,
    )

    with pytest.raises(CategorizedError):
        ObjectStore(client, "bucket").delete_batch(batch)
    assert [call["VersionId"] for call in client.delete_calls] == ["v1"]


def test_delete_retired_key_batch_returns_false_without_deleting_an_incomplete_latest() -> None:
    client = _VersionClient(
        [
            {
                "Versions": [
                    _version("p/key", "v1", latest=False),
                    _version("p/key", "v2", latest=True),
                ],
                "IsTruncated": True,
                "NextKeyMarker": "p/key",
                "NextVersionIdMarker": "v2",
            }
        ]
    )

    complete = ObjectStore(client, "bucket").delete_retired_key_batch("p/key", 2)

    assert complete is False
    assert [call["VersionId"] for call in client.delete_calls] == ["v1"]


def test_delete_batch_rejects_multiple_keys_or_latest_entries() -> None:
    store = ObjectStore(_VersionClient([]), "bucket")
    multiple_keys = VersionBatch(
        "p/key",
        (
            ObjectVersion("p/key", "v1", STORE_MTIME, "etag", False, False),
            ObjectVersion("p/other", "v2", STORE_MTIME, "etag", False, False),
        ),
        True,
    )
    multiple_latest = VersionBatch(
        "p/key",
        (
            ObjectVersion("p/key", "v1", STORE_MTIME, "etag", True, False),
            ObjectVersion("p/key", "v2", STORE_MTIME, "etag", True, False),
        ),
        True,
    )

    with pytest.raises(ValueError):
        store.delete_batch(multiple_keys)
    with pytest.raises(ValueError):
        store.delete_batch(multiple_latest)


def test_infrastructure_error_from_client_error_carries_s3_code() -> None:
    err = ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
    mapped = _infrastructure_error("put_object", "t/vmcore/oid/core", err)
    assert mapped.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(mapped) == "object-store put_object for 't/vmcore/oid/core' failed: AccessDenied"
    assert mapped.details == {"key": "t/vmcore/oid/core", "s3_error_code": "AccessDenied"}


def test_infrastructure_error_client_error_without_code_falls_back_to_unknown() -> None:
    err = ClientError({"Error": {}}, "PutObject")
    mapped = _infrastructure_error("put_object", "k", err)
    assert mapped.details == {"key": "k", "s3_error_code": "unknown"}
    assert str(mapped) == "object-store put_object for 'k' failed: unknown"


def test_infrastructure_error_client_error_without_error_block_is_unknown() -> None:
    # A ClientError whose response carries no "Error" mapping must still degrade to
    # "unknown" rather than crashing while building the typed failure.
    err = ClientError({}, "PutObject")
    mapped = _infrastructure_error("put_object", "k", err)
    assert mapped.details == {"key": "k", "s3_error_code": "unknown"}


def test_infrastructure_error_from_transport_error_uses_class_name() -> None:
    err = EndpointConnectionError(endpoint_url="http://unreachable")
    mapped = _infrastructure_error("get_object", "k", err)
    assert mapped.details == {"key": "k", "s3_error_code": "EndpointConnectionError"}
    assert str(mapped) == "object-store get_object for 'k' failed: EndpointConnectionError"


def test_local_stream_error_message_and_details() -> None:
    err = OSError(2, "No such file or directory")
    mapped = _local_stream_error("t/vmcore/oid/core", "/spool/core", err)
    assert mapped.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(mapped) == (
        "object-store put_stream for 't/vmcore/oid/core' could not read "
        "'/spool/core': No such file or directory"
    )
    assert mapped.details == {
        "op": "put_stream",
        "key": "t/vmcore/oid/core",
        "path": "/spool/core",
    }


@pytest.mark.parametrize(
    ("tenant", "kind", "object_id", "name"),
    [
        ("", "vmcore", "oid", "core"),
        ("t", "vmcore", "oid", ""),
        ("t", "with/slash", "oid", "core"),
        ("t", "vmcore", "oid", "bad\nname"),
    ],
)
def test_put_artifact_rejects_invalid_key_component(
    tenant: str, kind: str, object_id: str, name: str
) -> None:
    store = ObjectStore(object(), "bucket")  # client never touched: validation precedes it
    with pytest.raises(CategorizedError) as excinfo:
        store.put_artifact(
            ArtifactWriteRequest(
                tenant=tenant,
                owner_kind=kind,
                owner_id=object_id,
                name=name,
                data=b"x",
                sensitivity=Sensitivity.REDACTED,
                retention_class="vmcore",
            ),
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


class _UnreachableClient:
    """A stub S3 client whose calls raise a transport-level ``BotoCoreError``."""

    def put_object(self, **_kwargs: object) -> object:
        raise EndpointConnectionError(endpoint_url="http://unreachable")

    def get_object(self, **_kwargs: object) -> object:
        raise EndpointConnectionError(endpoint_url="http://unreachable")


def test_put_artifact_maps_transport_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_UnreachableClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.put_artifact(
            ArtifactWriteRequest(
                tenant="t",
                owner_kind="vmcore",
                owner_id="oid",
                name="core",
                data=b"x",
                sensitivity=Sensitivity.REDACTED,
                retention_class="vmcore",
            ),
        )
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == (
        "object-store put_object for 't/vmcore/oid/core' failed: EndpointConnectionError"
    )


class _RecordingPutClient:
    """Records the kwargs of its last ``put_object`` and returns a canned ETag."""

    def __init__(self) -> None:
        self.last_kwargs: dict[str, object] | None = None

    def put_object(self, **kwargs: object) -> dict[str, object]:
        body = kwargs.get("Body")
        if hasattr(body, "read"):
            kwargs["Body"] = body.read()  # ty: ignore[call-non-callable]
        self.last_kwargs = kwargs
        return {"ETag": '"stored-etag"', "VersionId": "put-version-1"}


class _CannedPutClient:
    def __init__(self, reply: dict[str, object]) -> None:
        self._reply = reply

    def put_object(self, **_kwargs: object) -> dict[str, object]:
        return self._reply


@pytest.mark.parametrize(
    "reply",
    [
        {"ETag": '"etag"'},
        {"ETag": '"etag"', "VersionId": ""},
        {"ETag": '"etag"', "VersionId": 1},
    ],
)
def test_put_artifact_rejects_missing_empty_or_malformed_version_id(
    reply: dict[str, object],
) -> None:
    store = ObjectStore(_CannedPutClient(reply), "bucket")
    request = ArtifactWriteRequest(
        tenant="t",
        owner_kind="runs",
        owner_id="r1",
        name="kernel",
        data=b"payload",
        sensitivity=Sensitivity.REDACTED,
        retention_class="build",
    )

    with pytest.raises(CategorizedError) as excinfo:
        store.put_artifact(request)

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_put_artifact_accepts_literal_null_version_id() -> None:
    store = ObjectStore(_CannedPutClient({"ETag": '"etag"', "VersionId": "null"}), "bucket")

    stored = store.put_artifact(
        ArtifactWriteRequest(
            tenant="t",
            owner_kind="runs",
            owner_id="r1",
            name="kernel",
            data=b"payload",
            sensitivity=Sensitivity.REDACTED,
            retention_class="build",
        )
    )

    assert stored.version_id == "null"


def test_put_artifact_writes_metadata_and_returns_stored_artifact() -> None:
    client = _RecordingPutClient()
    store = ObjectStore(client, "the-bucket")

    stored = store.put_artifact(
        ArtifactWriteRequest(
            tenant="t",
            owner_kind="vmcore",
            owner_id="oid",
            name="core",
            data=b"payload",
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="vmcore",
        )
    )

    assert client.last_kwargs is not None
    assert client.last_kwargs["Bucket"] == "the-bucket"
    assert client.last_kwargs["Key"] == "t/vmcore/oid/core"
    assert client.last_kwargs["Body"] == b"payload"
    assert client.last_kwargs["Metadata"] == {
        "sensitivity": "sensitive",
        "retention-class": "vmcore",
    }
    assert stored.key == "t/vmcore/oid/core"
    assert stored.etag == "stored-etag"  # the surrounding quotes are normalized off
    assert stored.sensitivity is Sensitivity.SENSITIVE
    assert stored.retention_class == "vmcore"
    assert stored.version_id == "put-version-1"


def test_put_artifact_passes_optional_sha256_checksum_to_s3() -> None:
    checksum = base64.b64encode(hashlib.sha256(b"payload").digest()).decode("ascii")
    client = _RecordingPutClient()

    ObjectStore(client, "the-bucket").put_artifact(
        ArtifactWriteRequest(
            tenant="t",
            owner_kind="vmcore",
            owner_id="oid",
            name="core",
            data=b"payload",
            sha256_b64=checksum,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="vmcore",
        )
    )

    assert client.last_kwargs is not None
    assert client.last_kwargs["ChecksumSHA256"] == checksum


def test_put_artifact_records_content_encoding_under_the_exact_metadata_key() -> None:
    # S3/MinIO lowercases user-metadata keys, so a real round-trip cannot tell "content-encoding"
    # from a miscased literal; assert the exact key sent to put_object to pin the mapping.
    client = _RecordingPutClient()
    stored = ObjectStore(client, "the-bucket").put_artifact(
        ArtifactWriteRequest(
            tenant="t",
            owner_kind="systems",
            owner_id="sys-1",
            name="console-part-0",
            data=b"x",
            sensitivity=Sensitivity.REDACTED,
            retention_class="evidence",
            content_encoding="gzip",
        )
    )
    assert client.last_kwargs is not None
    assert client.last_kwargs["Metadata"] == {
        "sensitivity": "redacted",
        "retention-class": "evidence",
        "content-encoding": "gzip",
    }
    assert stored.key == "t/systems/sys-1/console-part-0"
    assert stored.version_id == "put-version-1"


def _sha256_b64(path: Path) -> str:
    return base64.b64encode(hashlib.sha256(path.read_bytes()).digest()).decode("ascii")


def test_put_stream_rejects_invalid_key_component(tmp_path: Path) -> None:
    spool = tmp_path / "core"
    spool.write_bytes(b"x")
    store = ObjectStore(object(), "bucket")  # client never touched: validation precedes it
    with pytest.raises(CategorizedError) as excinfo:
        store.put_stream(
            ArtifactStreamRequest(
                tenant="t",
                owner_kind="with/slash",
                owner_id="oid",
                name="core",
                path=spool,
                sha256_b64=_sha256_b64(spool),
                sensitivity=Sensitivity.SENSITIVE,
                retention_class="vmcore",
            )
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_put_stream_maps_transport_error_to_infrastructure_failure(tmp_path: Path) -> None:
    spool = tmp_path / "core"
    spool.write_bytes(b"payload")
    store = ObjectStore(_UnreachableClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.put_stream(
            ArtifactStreamRequest(
                tenant="t",
                owner_kind="vmcore",
                owner_id="oid",
                name="core",
                path=spool,
                sha256_b64=_sha256_b64(spool),
                sensitivity=Sensitivity.SENSITIVE,
                retention_class="vmcore",
            )
        )
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == (
        "object-store put_object for 't/vmcore/oid/core' failed: EndpointConnectionError"
    )


def test_put_stream_writes_checksum_metadata_and_returns_stored_artifact(tmp_path: Path) -> None:
    spool = tmp_path / "core.kdump"
    spool.write_bytes(b"spooled-bytes")
    checksum = _sha256_b64(spool)
    client = _RecordingPutClient()
    store = ObjectStore(client, "the-bucket")

    stored = store.put_stream(
        ArtifactStreamRequest(
            tenant="t",
            owner_kind="systems",
            owner_id="sys-1",
            name="vmcore",
            path=spool,
            sha256_b64=checksum,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="vmcore",
        )
    )

    assert client.last_kwargs is not None
    assert client.last_kwargs["Key"] == "t/systems/sys-1/vmcore"
    assert client.last_kwargs["Body"] == b"spooled-bytes"
    assert client.last_kwargs["ChecksumSHA256"] == checksum
    assert client.last_kwargs["Metadata"] == {
        "sensitivity": "sensitive",
        "retention-class": "vmcore",
    }
    assert stored.etag == "stored-etag"
    assert stored.sensitivity is Sensitivity.SENSITIVE
    assert stored.retention_class == "vmcore"
    assert stored.version_id == "put-version-1"


@pytest.mark.parametrize(
    "reply",
    [
        {"ETag": '"etag"'},
        {"ETag": '"etag"', "VersionId": ""},
        {"ETag": '"etag"', "VersionId": 1},
    ],
)
def test_put_stream_rejects_missing_empty_or_malformed_version_id(
    tmp_path: Path, reply: dict[str, object]
) -> None:
    spool = tmp_path / "core"
    spool.write_bytes(b"payload")
    store = ObjectStore(_CannedPutClient(reply), "bucket")

    with pytest.raises(CategorizedError) as excinfo:
        store.put_stream(
            ArtifactStreamRequest(
                tenant="t",
                owner_kind="runs",
                owner_id="r1",
                name="kernel",
                path=spool,
                sha256_b64=_sha256_b64(spool),
                sensitivity=Sensitivity.REDACTED,
                retention_class="build",
            )
        )

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_put_stream_accepts_literal_null_version_id(tmp_path: Path) -> None:
    spool = tmp_path / "core"
    spool.write_bytes(b"payload")
    store = ObjectStore(_CannedPutClient({"ETag": '"etag"', "VersionId": "null"}), "bucket")

    stored = store.put_stream(
        ArtifactStreamRequest(
            tenant="t",
            owner_kind="runs",
            owner_id="r1",
            name="kernel",
            path=spool,
            sha256_b64=_sha256_b64(spool),
            sensitivity=Sensitivity.REDACTED,
            retention_class="build",
        )
    )

    assert stored.version_id == "null"


def test_put_stream_maps_local_source_error_to_infrastructure_failure(tmp_path: Path) -> None:
    missing = tmp_path / "missing-core"
    request = ArtifactStreamRequest(
        tenant="t",
        owner_kind="vmcore",
        owner_id="oid",
        name="core",
        path=missing,
        sha256_b64="unused",
        sensitivity=Sensitivity.SENSITIVE,
        retention_class="vmcore",
    )
    store = ObjectStore(object(), "bucket")

    with pytest.raises(CategorizedError) as excinfo:
        store.put_stream(request)

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert excinfo.value.details == {
        "op": "put_stream",
        "key": "t/vmcore/oid/core",
        "path": str(missing),
    }
    assert isinstance(excinfo.value.__cause__, OSError)


def test_get_artifact_maps_transport_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_UnreachableClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.get_artifact("t/vmcore/oid/core", "etag")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == (
        "object-store get_object for 't/vmcore/oid/core' failed: EndpointConnectionError"
    )


def _client_error(status: int, code: str = "x") -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "GetObject"
    )


class _StatusErrorClient:
    """A stub whose object operations raise a ``ClientError`` with a fixed HTTP status."""

    def __init__(self, status: int) -> None:
        self._err = _client_error(status)

    def get_object(self, **_kwargs: object) -> dict[str, object]:
        raise self._err

    def head_object(self, **_kwargs: object) -> dict[str, object]:
        raise self._err


@pytest.mark.parametrize("status", [404, 412])
def test_get_artifact_stale_statuses_raise_stale_handle(status: int) -> None:
    store = ObjectStore(_StatusErrorClient(status), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.get_artifact("t/vmcore/oid/core", "etag")
    assert excinfo.value.category is ErrorCategory.STALE_HANDLE
    assert excinfo.value.details == {"key": "t/vmcore/oid/core", "http_status": status}
    assert str(excinfo.value) == (
        "artifact 't/vmcore/oid/core' is gone or its etag no longer matches"
    )


def test_get_artifact_non_stale_client_error_is_infrastructure_failure() -> None:
    store = ObjectStore(_StatusErrorClient(500), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.get_artifact("k", "etag")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    # A non-404/412 GET failure carries the op, key, and S3 error code verbatim so an operator
    # can tell an access/quota fault from a stale handle.
    assert str(excinfo.value) == "object-store get_object for 'k' failed: x"
    assert excinfo.value.details == {"key": "k", "s3_error_code": "x"}


def test_get_artifact_client_error_without_response_metadata_is_infrastructure_failure() -> None:
    # A ClientError whose response omits ResponseMetadata (no HTTP status to read) is not a
    # stale handle: it must still map to INFRASTRUCTURE_FAILURE rather than crash reading a
    # status off a missing block.
    class _NoMetadataClient:
        def get_object(self, **_kwargs: object) -> dict[str, object]:
            raise ClientError({"Error": {"Code": "boom"}}, "GetObject")

    store = ObjectStore(_NoMetadataClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.get_artifact("k", "etag")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == "object-store get_object for 'k' failed: boom"


def test_get_artifact_invalid_metadata_is_infrastructure_failure() -> None:
    class _BadMetaClient:
        def get_object(self, **_kwargs: object) -> dict[str, object]:
            return {"Metadata": {}, "Body": _StaticBody(b"x")}

    store = ObjectStore(_BadMetaClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.get_artifact("t/vmcore/oid/core", None)
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert excinfo.value.details == {"key": "t/vmcore/oid/core"}
    assert str(excinfo.value) == (
        "artifact 't/vmcore/oid/core' has absent or invalid sensitivity metadata"
    )


def test_head_404_returns_none_other_status_raises() -> None:
    assert ObjectStore(_StatusErrorClient(404), "bucket").head("k") is None
    with pytest.raises(CategorizedError) as excinfo:
        ObjectStore(_StatusErrorClient(500), "bucket").head("k")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == "object-store head_object for 'k' failed: x"


def test_head_maps_transport_error_to_infrastructure_failure() -> None:
    # A transport-level BotoCoreError on HEAD is not a 404: it maps to INFRASTRUCTURE_FAILURE
    # with the op, key, and error class named, distinct from the "object absent" None return.
    class _UnreachableHeadClient:
        def head_object(self, **_kwargs: object) -> dict[str, object]:
            raise EndpointConnectionError(endpoint_url="http://unreachable")

    with pytest.raises(CategorizedError) as excinfo:
        ObjectStore(_UnreachableHeadClient(), "bucket").head("k")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == "object-store head_object for 'k' failed: EndpointConnectionError"
    assert excinfo.value.details == {"key": "k", "s3_error_code": "EndpointConnectionError"}


def test_head_client_error_without_response_metadata_is_infrastructure_failure() -> None:
    # A ClientError whose response omits ResponseMetadata has no HTTP status to read: HEAD must
    # not mistake it for a 404 (returning None) nor crash reading a status off a missing block.
    class _NoMetadataHeadClient:
        def head_object(self, **_kwargs: object) -> dict[str, object]:
            raise ClientError({"Error": {"Code": "boom"}}, "HeadObject")

    with pytest.raises(CategorizedError) as excinfo:
        ObjectStore(_NoMetadataHeadClient(), "bucket").head("k")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == "object-store head_object for 'k' failed: boom"


def test_head_invalid_sensitivity_metadata_returns_unknown_sensitivity() -> None:
    class _BadHeadMetaClient:
        def head_object(self, **_kwargs: object) -> dict[str, object]:
            return {
                "ContentLength": 1,
                "ETag": '"etag"',
                "LastModified": STORE_MTIME,
                "VersionId": "head-version-1",
                "Metadata": {"sensitivity": "bogus"},
            }

    head = ObjectStore(_BadHeadMetaClient(), "bucket").head("k")

    assert head is not None
    assert head.sensitivity is None


class _MidStreamFailureClient:
    """A stub whose ``get_object`` succeeds but whose body read fails mid-stream."""

    class _Body:
        def read(self) -> bytes:
            raise ReadTimeoutError(endpoint_url="http://unreachable")

    def get_object(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Metadata": {"sensitivity": "redacted", "retention-class": "vmcore"},
            "Body": _MidStreamFailureClient._Body(),
        }


def test_get_artifact_maps_body_read_failure_to_infrastructure_failure() -> None:
    store = ObjectStore(_MidStreamFailureClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.get_artifact("t/vmcore/oid/core", "etag")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    # The op label, the key, and the boto error class all ride into the message/details, so a
    # mid-stream body-read fault is attributable: pin the whole message and the carried code.
    assert str(excinfo.value) == (
        "object-store get_object for 't/vmcore/oid/core' failed: ReadTimeoutError"
    )
    assert excinfo.value.details == {
        "key": "t/vmcore/oid/core",
        "s3_error_code": "ReadTimeoutError",
    }


class _RecordingClient:
    """A stub S3 client that records the kwargs of its last ``get_object`` call."""

    def __init__(self) -> None:
        self.last_kwargs: dict[str, object] | None = None

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.last_kwargs = kwargs
        return {
            "Metadata": {"sensitivity": "redacted", "retention-class": "vmcore"},
            "Body": _StaticBody(b"bytes"),
        }


class _StaticBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def test_get_artifact_with_etag_sends_if_match() -> None:
    client = _RecordingClient()
    store = ObjectStore(client, "bucket")

    store.get_artifact("t/vmcore/oid/core", "abc123")

    assert client.last_kwargs is not None
    assert client.last_kwargs.get("IfMatch") == '"abc123"'


def test_get_artifact_none_etag_omits_if_match() -> None:
    client = _RecordingClient()
    store = ObjectStore(client, "bucket")

    fetched = store.get_artifact("t/vmcore/oid/core", None)

    assert client.last_kwargs is not None
    assert "IfMatch" not in client.last_kwargs
    assert fetched.data == b"bytes"


class _StreamingBody:
    """Fake boto ``StreamingBody``: ``read(size)`` returns up to ``size`` bytes (``b""`` at
    true EOF) and ``close()`` records the close, mirroring the real body the reader wraps."""

    def __init__(self, data: bytes, *, chunk: int | None = None) -> None:
        self._buf = data
        self._pos = 0
        self._chunk = chunk
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        remaining = len(self._buf) - self._pos
        want = remaining if size is None or size < 0 else min(size, remaining)
        if self._chunk is not None:
            want = min(want, self._chunk)
        chunk = self._buf[self._pos : self._pos + want]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class _StreamingClient:
    """A stub S3 client whose ``get_object`` returns a fresh ``_StreamingBody`` + metadata."""

    def __init__(
        self, data: bytes, *, chunk: int | None = None, metadata: dict[str, str] | None = None
    ) -> None:
        self._data = data
        self._chunk = chunk
        self._metadata = (
            metadata
            if metadata is not None
            else {"sensitivity": "redacted", "retention-class": "vmcore"}
        )
        self.last_kwargs: dict[str, object] | None = None
        self.body: _StreamingBody | None = None

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.last_kwargs = kwargs
        self.body = _StreamingBody(self._data, chunk=self._chunk)
        return {"Metadata": self._metadata, "Body": self.body}


class _StreamErrorBody:
    """A body whose read raises a transport error, to drive the reader's mid-stream mapping."""

    def read(self, _size: int = -1) -> bytes:
        raise ReadTimeoutError(endpoint_url="http://unreachable")

    def close(self) -> None:
        pass


class _StreamErrorClient:
    def get_object(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Metadata": {"sensitivity": "redacted", "retention-class": "vmcore"},
            "Body": _StreamErrorBody(),
        }


def test_get_artifact_stream_yields_body_and_metadata() -> None:
    store = ObjectStore(_StreamingClient(b"combined-tar-bytes"), "bucket")
    with store.get_artifact_stream("t/runs/run-1/kernel", None) as streamed:
        assert streamed.sensitivity is Sensitivity.REDACTED
        assert streamed.retention_class == "vmcore"
        assert streamed.reader.read() == b"combined-tar-bytes"


def test_get_artifact_stream_short_reads_reassemble_without_truncation() -> None:
    # The body hands back one byte per read; a correct readinto must not read a short chunk
    # as EOF, or the streamed tar would silently truncate.
    store = ObjectStore(_StreamingClient(b"one-byte-at-a-time", chunk=1), "bucket")
    with store.get_artifact_stream("k", None) as streamed:
        assert streamed.reader.read() == b"one-byte-at-a-time"


def test_get_artifact_stream_closes_body_on_exit() -> None:
    client = _StreamingClient(b"payload")
    with ObjectStore(client, "bucket").get_artifact_stream("k", None) as streamed:
        streamed.reader.read(1)
    assert client.body is not None
    assert client.body.closed


@pytest.mark.parametrize("status", [404, 412])
def test_get_artifact_stream_stale_statuses_raise_stale_handle(status: int) -> None:
    store = ObjectStore(_StatusErrorClient(status), "bucket")
    with (
        pytest.raises(CategorizedError) as excinfo,
        store.get_artifact_stream("t/vmcore/oid/core", "etag"),
    ):
        pass
    assert excinfo.value.category is ErrorCategory.STALE_HANDLE
    assert excinfo.value.details == {"key": "t/vmcore/oid/core", "http_status": status}


def test_get_artifact_stream_non_stale_client_error_is_infrastructure_failure() -> None:
    store = ObjectStore(_StatusErrorClient(500), "bucket")
    with pytest.raises(CategorizedError) as excinfo, store.get_artifact_stream("k", None):
        pass
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_get_artifact_stream_transport_error_is_infrastructure_failure() -> None:
    store = ObjectStore(_UnreachableClient(), "bucket")
    with (
        pytest.raises(CategorizedError) as excinfo,
        store.get_artifact_stream("t/vmcore/oid/core", None),
    ):
        pass
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_get_artifact_stream_invalid_metadata_is_infrastructure_failure() -> None:
    store = ObjectStore(_StreamingClient(b"x", metadata={}), "bucket")
    with (
        pytest.raises(CategorizedError) as excinfo,
        store.get_artifact_stream("t/vmcore/oid/core", None),
    ):
        pass
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert excinfo.value.details == {"key": "t/vmcore/oid/core"}


def test_get_artifact_stream_mid_read_error_maps_to_infrastructure_failure() -> None:
    store = ObjectStore(_StreamErrorClient(), "bucket")
    with (
        pytest.raises(CategorizedError) as excinfo,
        store.get_artifact_stream("t/vmcore/oid/core", None) as streamed,
    ):
        streamed.reader.read()
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert excinfo.value.details["key"] == "t/vmcore/oid/core"
    # The boto error class is carried as s3_error_code, so a read-timeout is observably distinct
    # from a dropped connection without a new field (ADR-0400 residual observability).
    assert excinfo.value.details["s3_error_code"] == "ReadTimeoutError"
    # The reader labels its own op "get_object": the whole message stays attributable through
    # tarfile's stream buffering back to the failed download.
    assert str(excinfo.value) == (
        "object-store get_object for 't/vmcore/oid/core' failed: ReadTimeoutError"
    )


def test_get_artifact_stream_with_etag_sends_if_match() -> None:
    client = _StreamingClient(b"x")
    with ObjectStore(client, "bucket").get_artifact_stream("k", "abc123"):
        pass
    assert client.last_kwargs is not None
    assert client.last_kwargs.get("IfMatch") == '"abc123"'


def test_get_artifact_stream_none_etag_omits_if_match() -> None:
    client = _StreamingClient(b"x")
    with ObjectStore(client, "bucket").get_artifact_stream("k", None):
        pass
    assert client.last_kwargs is not None
    assert "IfMatch" not in client.last_kwargs


def test_register_artifact_row_maps_stored_and_owner() -> None:
    stored = StoredArtifact(
        "t/vmcore/oid/core", "etag123", Sensitivity.REDACTED, "vmcore", "test-version-1"
    )
    owner_id = uuid4()

    row = register_artifact_row(stored, owner_kind="system", owner_id=owner_id)

    assert row.object_key == "t/vmcore/oid/core"
    assert row.etag == "etag123"
    assert row.sensitivity is Sensitivity.REDACTED
    assert row.retention_class == "vmcore"
    assert row.owner_kind == "system"
    assert row.owner_id == owner_id
    # id is minted; created_at/updated_at are populated (advisory pre-insert) and tz-aware UTC.
    assert row.id is not None
    assert row.created_at.tzinfo is UTC
    assert row.updated_at.tzinfo is UTC


def test_object_store_from_env_requires_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("KDIVE_S3_BUCKET", "bucket")

    with pytest.raises(CategorizedError) as excinfo:
        object_store_from_env()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(excinfo.value) == ("KDIVE_S3_ENDPOINT_URL is not set; cannot reach the object store")


def test_object_store_from_env_requires_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.delenv("KDIVE_S3_BUCKET", raising=False)

    with pytest.raises(CategorizedError) as excinfo:
        object_store_from_env()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(excinfo.value) == "KDIVE_S3_BUCKET is not set; cannot reach the object store"


class _VersioningClient:
    def __init__(self, reply: dict[str, object] | Exception) -> None:
        self._reply = reply
        self.head_calls = 0
        self.versioning_calls = 0

    def head_bucket(self, **_kwargs: object) -> None:
        self.head_calls += 1

    def get_bucket_versioning(self, **_kwargs: object) -> dict[str, object]:
        self.versioning_calls += 1
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


@pytest.mark.parametrize("mfa_delete", [None, "Disabled"])
def test_validate_versioning_accepts_enabled_bucket_with_absent_or_disabled_mfa_delete(
    mfa_delete: str | None,
) -> None:
    reply: dict[str, object] = {"Status": "Enabled"}
    if mfa_delete is not None:
        reply["MFADelete"] = mfa_delete
    client = _VersioningClient(reply)
    ObjectStore(client, "bucket").validate_versioning()
    assert client.versioning_calls == 1


@pytest.mark.parametrize("status", [None, "Suspended"])
def test_validate_versioning_rejects_missing_or_suspended_versioning(status: str | None) -> None:
    reply: dict[str, object] = {} if status is None else {"Status": status}
    with pytest.raises(CategorizedError) as excinfo:
        ObjectStore(_VersioningClient(reply), "bucket").validate_versioning()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "bucket" in str(excinfo.value) and "Enabled" in str(excinfo.value)


def test_mfa_delete_enabled_is_configuration_error() -> None:
    store = ObjectStore(_VersioningClient({"Status": "Enabled", "MFADelete": "Enabled"}), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.validate_versioning()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "dedicated bucket" in str(excinfo.value)


@pytest.mark.parametrize(
    "reply",
    [
        {"Status": 1},
        {"Status": "Enabled", "MFADelete": 1},
        {"Status": "Enabled", "MFADelete": ""},
        {"Status": "Enabled", "MFADelete": "Bogus"},
    ],
)
def test_validate_versioning_rejects_malformed_reply(reply: dict[str, object]) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        ObjectStore(_VersioningClient(reply), "bucket").validate_versioning()
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_validate_versioning_maps_client_fault_to_infrastructure_failure() -> None:
    store = ObjectStore(
        _VersioningClient(EndpointConnectionError(endpoint_url="http://x")), "bucket"
    )
    with pytest.raises(CategorizedError) as excinfo:
        store.validate_versioning()
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_ping_revalidates_versioning_after_head_bucket() -> None:
    client = _VersioningClient({"Status": "Enabled"})
    ObjectStore(client, "bucket").ping()
    assert client.head_calls == 1 and client.versioning_calls == 1


def test_object_store_from_env_defaults_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("KDIVE_S3_BUCKET", "bucket")
    monkeypatch.delenv("KDIVE_S3_REGION", raising=False)

    client = _VersioningClient({"Status": "Enabled"})
    client_kwargs: dict[str, object] = {}

    def _client_factory(*args: object, **kwargs: object) -> _VersioningClient:
        assert args == ("s3",)
        client_kwargs.update(kwargs)
        return client

    monkeypatch.setattr("kdive.store.objectstore.boto3.client", _client_factory)
    store = object_store_from_env()

    assert store._client is client
    assert store._bucket == "bucket"
    assert client.versioning_calls == 1
    assert client_kwargs == {"endpoint_url": "http://localhost:9000", "region_name": "us-east-1"}


def test_object_store_from_env_uses_configured_region(monkeypatch: pytest.MonkeyPatch) -> None:
    # A configured region is honored verbatim (not collapsed to the default), and the
    # configured endpoint/bucket flow through to the constructed client and store.
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://minio.internal:9000")
    monkeypatch.setenv("KDIVE_S3_BUCKET", "artifacts")
    monkeypatch.setenv("KDIVE_S3_REGION", "eu-west-1")

    client = _VersioningClient({"Status": "Enabled"})
    client_kwargs: dict[str, object] = {}

    def _client_factory(*args: object, **kwargs: object) -> _VersioningClient:
        assert args == ("s3",)
        client_kwargs.update(kwargs)
        return client

    monkeypatch.setattr("kdive.store.objectstore.boto3.client", _client_factory)
    store = object_store_from_env()

    assert store._client is client
    assert store._bucket == "artifacts"
    assert client.versioning_calls == 1
    assert client_kwargs == {
        "endpoint_url": "http://minio.internal:9000",
        "region_name": "eu-west-1",
    }


def test_put_get_round_trip(minio_store: ObjectStore, key_ns: str) -> None:
    stored = minio_store.put_artifact(
        ArtifactWriteRequest(
            tenant=key_ns,
            owner_kind="vmcore",
            owner_id="sys-1",
            name="core.bin",
            data=b"payload-bytes",
            sensitivity=Sensitivity.REDACTED,
            retention_class="vmcore",
        ),
    )

    assert '"' not in stored.etag  # stored etag is the bare value
    fetched = minio_store.get_artifact(stored.key, stored.etag)
    assert fetched.data == b"payload-bytes"


def test_minio_lists_versions_and_markers_then_deletes_one_exact_identity(
    minio_store: ObjectStore, key_ns: str
) -> None:
    key = f"{key_ns}/runs/run-1/versioned"
    first = minio_store._client.put_object(Bucket=minio_store._bucket, Key=key, Body=b"first")
    second = minio_store._client.put_object(Bucket=minio_store._bucket, Key=key, Body=b"second")
    marker = minio_store._client.delete_object(Bucket=minio_store._bucket, Key=key)

    page = minio_store.list_version_page(key)

    assert {entry.version_id for entry in page.entries} == {
        first["VersionId"],
        second["VersionId"],
        marker["VersionId"],
    }
    assert any(entry.is_delete_marker and entry.is_latest for entry in page.entries)
    assert any(not entry.is_delete_marker and not entry.is_latest for entry in page.entries)

    minio_store.delete_version(key, first["VersionId"])

    remaining = minio_store.list_version_page(key)
    assert first["VersionId"] not in {entry.version_id for entry in remaining.entries}
    assert {second["VersionId"], marker["VersionId"]} <= {
        entry.version_id for entry in remaining.entries
    }


def test_minio_public_put_and_head_expose_the_same_version_id(
    minio_store: ObjectStore, key_ns: str
) -> None:
    stored = minio_store.put_artifact(
        ArtifactWriteRequest(
            tenant=key_ns,
            owner_kind="runs",
            owner_id="run-1",
            name="version-id-round-trip",
            data=b"payload",
            sensitivity=Sensitivity.REDACTED,
            retention_class="build",
        )
    )

    head = minio_store.head(stored.key)

    assert stored.version_id
    assert head is not None
    assert head.version_id == stored.version_id


def test_minio_suspended_versioning_exposes_legacy_null_when_supported(
    minio_store: ObjectStore, key_ns: str
) -> None:
    key = f"{key_ns}/runs/run-1/legacy-null-version"
    wrote_legacy_version = False
    deleted_legacy_version = False
    observed_version_ids: tuple[str, ...] = ()
    try:
        with _suspend_versioning_or_skip(minio_store):
            minio_store._client.put_object(Bucket=minio_store._bucket, Key=key, Body=b"payload")
            wrote_legacy_version = True
            observed_version_ids = _legacy_inventory_ids_or_skip(minio_store, key)
            assert observed_version_ids == ("null",)
            minio_store.delete_version(key, "null")
            deleted_legacy_version = True
            observed_version_ids = ()
    finally:
        for version_id in observed_version_ids:
            minio_store.delete_version(key, version_id)
        if wrote_legacy_version and not observed_version_ids and not deleted_legacy_version:
            minio_store.delete_version(key, "null")


def test_put_stream_round_trip_streams_from_disk(
    minio_store: ObjectStore, key_ns: str, tmp_path: Path
) -> None:
    spool = tmp_path / "core.kdump"
    spool.write_bytes(b"spooled-core-bytes")
    stored = minio_store.put_stream(
        ArtifactStreamRequest(
            tenant=key_ns,
            owner_kind="systems",
            owner_id="sys-1",
            name="vmcore-host_dump",
            path=spool,
            sha256_b64=_sha256_b64(spool),
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="vmcore",
        )
    )

    assert '"' not in stored.etag
    fetched = minio_store.get_artifact(stored.key, stored.etag)
    assert fetched.data == b"spooled-core-bytes"
    assert fetched.sensitivity is Sensitivity.SENSITIVE


def test_get_artifact_unconditional_reads_without_etag(
    minio_store: ObjectStore, key_ns: str
) -> None:
    stored = minio_store.put_artifact(
        ArtifactWriteRequest(
            tenant=key_ns,
            owner_kind="runs",
            owner_id="run-1",
            name="kernel",
            data=b"bzimage-bytes",
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
        ),
    )

    fetched = minio_store.get_artifact(stored.key, None)
    assert fetched.data == b"bzimage-bytes"
    assert fetched.sensitivity is Sensitivity.SENSITIVE


def test_get_artifact_unconditional_missing_key_raises_stale_handle(
    minio_store: ObjectStore, key_ns: str
) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        minio_store.get_artifact(f"{key_ns}/runs/none/kernel", None)
    assert excinfo.value.category is ErrorCategory.STALE_HANDLE


def test_get_artifact_stream_minio_round_trip(minio_store: ObjectStore, key_ns: str) -> None:
    # AC1: the streamed bytes are byte-identical to the buffered get_artifact bytes, with the
    # same sensitivity/retention_class read from the same object metadata.
    stored = minio_store.put_artifact(
        ArtifactWriteRequest(
            tenant=key_ns,
            owner_kind="runs",
            owner_id="run-1",
            name="kernel",
            data=b"combined-tar-payload",
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
        ),
    )
    buffered = minio_store.get_artifact(stored.key, None)
    with minio_store.get_artifact_stream(stored.key, None) as streamed:
        streamed_bytes = streamed.reader.read()
    assert streamed_bytes == buffered.data == b"combined-tar-payload"
    assert streamed.sensitivity is buffered.sensitivity is Sensitivity.SENSITIVE
    assert streamed.retention_class == buffered.retention_class == "build"


def test_put_uses_the_key_scheme(minio_store: ObjectStore, key_ns: str) -> None:
    stored = minio_store.put_artifact(
        ArtifactWriteRequest(
            tenant=key_ns,
            owner_kind="vmcore",
            owner_id="oid",
            name="core",
            data=b"x",
            sensitivity=Sensitivity.REDACTED,
            retention_class="vmcore",
        ),
    )
    assert stored.key == f"{key_ns}/vmcore/oid/core"


def test_sensitivity_persisted_as_object_metadata(minio_store: ObjectStore, key_ns: str) -> None:
    stored = minio_store.put_artifact(
        ArtifactWriteRequest(
            tenant=key_ns,
            owner_kind="transcript",
            owner_id="sys-1",
            name="gdb.log",
            data=b"raw-transcript",
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="transcript",
        ),
    )

    fetched = minio_store.get_artifact(stored.key, stored.etag)
    assert fetched.sensitivity is Sensitivity.SENSITIVE
    assert fetched.retention_class == "transcript"

    raw = minio_store._client.head_object(Bucket=minio_store._bucket, Key=stored.key)
    assert raw["Metadata"]["sensitivity"] == "sensitive"
    assert raw["Metadata"]["retention-class"] == "transcript"

    # head() surfaces the object's class without fetching the body (ADR-0140 gate).
    head = minio_store.head(stored.key)
    assert head is not None
    assert head.sensitivity is Sensitivity.SENSITIVE


def test_get_with_stale_etag_raises_stale_handle(minio_store: ObjectStore, key_ns: str) -> None:
    stored = minio_store.put_artifact(
        ArtifactWriteRequest(
            tenant=key_ns,
            owner_kind="vmcore",
            owner_id="sys-1",
            name="core.bin",
            data=b"payload",
            sensitivity=Sensitivity.REDACTED,
            retention_class="vmcore",
        ),
    )

    with pytest.raises(CategorizedError) as excinfo:
        minio_store.get_artifact(stored.key, "0" * 32)
    assert excinfo.value.category is ErrorCategory.STALE_HANDLE


def test_get_missing_object_raises_stale_handle(minio_store: ObjectStore, key_ns: str) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        minio_store.get_artifact(f"{key_ns}/vmcore/none/missing", "abc123")
    assert excinfo.value.category is ErrorCategory.STALE_HANDLE


def test_get_object_without_metadata_raises_infrastructure_failure(
    minio_store: ObjectStore, key_ns: str
) -> None:
    key = f"{key_ns}/vmcore/sys-1/bare"
    resp = minio_store._client.put_object(Bucket=minio_store._bucket, Key=key, Body=b"no-metadata")
    etag = resp["ETag"].strip('"')

    with pytest.raises(CategorizedError) as excinfo:
        minio_store.get_artifact(key, etag)
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


class _FakePresignClient:
    """Records ``generate_presigned_url`` calls; pure unit seam (no MinIO needed)."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.minted_url = "https://store.example/presigned"
        self.calls: list[tuple[str, dict[str, str], int, str]] = []
        self._raises = raises

    def generate_presigned_url(
        self, op: str, *, Params: dict[str, str], ExpiresIn: int, HttpMethod: str
    ) -> str:
        if self._raises is not None:
            raise self._raises
        self.calls.append((op, Params, ExpiresIn, HttpMethod))
        return self.minted_url


def test_presign_get_mints_time_boxed_url_for_one_key() -> None:
    client = _FakePresignClient()
    store = ObjectStore(client, "bucket")
    url = store.presign_get("t/vmcore/abc/core", expires_in=600)
    assert url == client.minted_url
    assert client.calls == [
        ("get_object", {"Bucket": "bucket", "Key": "t/vmcore/abc/core"}, 600, "GET")
    ]


@pytest.mark.parametrize("expires_in", [0, -1])
def test_presign_get_rejects_non_positive_expiry(expires_in: int) -> None:
    store = ObjectStore(_FakePresignClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.presign_get("k", expires_in=expires_in)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details == {"key": "k"}
    assert str(excinfo.value) == (f"presign_get for 'k' needs a positive expiry, got {expires_in}")


def test_presign_get_accepts_smallest_positive_expiry() -> None:
    # The boundary is strictly > 0: a 1-second expiry is the smallest accepted value.
    client = _FakePresignClient()
    store = ObjectStore(client, "bucket")
    assert store.presign_get("k", expires_in=1) == client.minted_url


def test_presign_put_signs_checksum_and_metadata_into_url() -> None:
    from kdive.artifacts.storage import PresignPutRequest

    client = _FakePresignClient()
    store = ObjectStore(client, "the-bucket")
    upload = store.presign_put(
        PresignPutRequest(
            key="t/vmcore/oid/core",
            sha256="abc-checksum",
            size_bytes=1024,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="vmcore",
            expires_in=900,
        )
    )

    assert upload.url == client.minted_url
    assert upload.required_headers == {
        "x-amz-checksum-sha256": "abc-checksum",
        "x-amz-meta-sensitivity": "sensitive",
        "x-amz-meta-retention-class": "vmcore",
    }
    op, params, expires_in, http_method = client.calls[0]
    assert op == "put_object"
    assert http_method == "PUT"
    assert expires_in == 900
    assert params == {
        "Bucket": "the-bucket",
        "Key": "t/vmcore/oid/core",
        "ChecksumSHA256": "abc-checksum",
        "Metadata": {"sensitivity": "sensitive", "retention-class": "vmcore"},
    }


def test_presign_put_maps_client_error_to_infrastructure_failure() -> None:
    from kdive.artifacts.storage import PresignPutRequest

    err = ClientError({"Error": {"Code": "boom"}}, "presign")
    store = ObjectStore(_FakePresignClient(raises=err), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.presign_put(
            PresignPutRequest(
                key="k",
                sha256="x",
                size_bytes=10,
                sensitivity=Sensitivity.REDACTED,
                retention_class="vmcore",
                expires_in=60,
            )
        )
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == "object-store presign_put for 'k' failed: boom"


def test_presign_get_maps_client_error_to_infrastructure_failure() -> None:
    err = ClientError({"Error": {"Code": "boom"}}, "presign")
    store = ObjectStore(_FakePresignClient(raises=err), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.presign_get("k", expires_in=60)
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == "object-store presign_get for 'k' failed: boom"


class _MpuClient:
    """Records the multipart calls so the reassembly primitives can be asserted in isolation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def create_multipart_upload(self, **kw: object) -> dict[str, object]:
        self.calls.append(("create", kw))
        return {"UploadId": "uid-1"}

    def upload_part_copy(self, **kw: object) -> dict[str, object]:
        self.calls.append(("copy", kw))
        return {"CopyPartResult": {"ETag": f'"etag-{kw["PartNumber"]}"'}}

    def complete_multipart_upload(self, **kw: object) -> dict[str, object]:
        self.calls.append(("complete", kw))
        return {"ETag": '"final-etag"'}

    def abort_multipart_upload(self, **kw: object) -> None:
        self.calls.append(("abort", kw))


def test_multipart_reassembly_primitives_round_trip() -> None:
    client = _MpuClient()
    store = ObjectStore(client, "bucket")
    uid = store.create_multipart_upload(
        "local/runs/x/vmlinux", sensitivity=Sensitivity.SENSITIVE, retention_class="build"
    )
    assert uid == "uid-1"
    assert client.calls[0][1]["Metadata"] == {
        "sensitivity": "sensitive",
        "retention-class": "build",
    }
    etag1 = store.upload_part_copy(
        "local/runs/x/vmlinux", uid, part_number=1, source_key="local/runs/x/vmlinux.part0001"
    )
    assert etag1 == "etag-1"
    assert client.calls[1][1]["CopySource"] == {
        "Bucket": "bucket",
        "Key": "local/runs/x/vmlinux.part0001",
    }
    final = store.complete_multipart_upload("local/runs/x/vmlinux", uid, [(1, "etag-1")])
    assert final == "final-etag"
    assert client.calls[2][1]["MultipartUpload"] == {"Parts": [{"PartNumber": 1, "ETag": "etag-1"}]}
    store.abort_multipart_upload("local/runs/x/vmlinux", uid)
    assert client.calls[3][0] == "abort"


def test_multipart_create_maps_client_error_to_infrastructure() -> None:
    class _Raises:
        def create_multipart_upload(self, **_: object) -> dict[str, object]:
            raise ClientError({"Error": {"Code": "boom"}}, "create_multipart_upload")

    store = ObjectStore(_Raises(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.create_multipart_upload(
            "k", sensitivity=Sensitivity.SENSITIVE, retention_class="build"
        )
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == "object-store create_multipart_upload for 'k' failed: boom"


def test_multipart_calls_target_the_bound_bucket_key_and_upload() -> None:
    # Every multipart call must address the store's bound bucket and the caller's key /
    # upload-id; a dropped or null binding would target the wrong object.
    client = _MpuClient()
    store = ObjectStore(client, "the-bucket")
    uid = store.create_multipart_upload(
        "runs/x/vmlinux", sensitivity=Sensitivity.SENSITIVE, retention_class="build"
    )
    store.upload_part_copy("runs/x/vmlinux", uid, part_number=1, source_key="runs/x/part1")
    store.complete_multipart_upload("runs/x/vmlinux", uid, [(1, "etag-1")])
    store.abort_multipart_upload("runs/x/vmlinux", uid)

    create_kw = client.calls[0][1]
    assert create_kw["Bucket"] == "the-bucket"
    assert create_kw["Key"] == "runs/x/vmlinux"
    copy_kw = client.calls[1][1]
    assert copy_kw["Bucket"] == "the-bucket"
    assert copy_kw["Key"] == "runs/x/vmlinux"
    assert copy_kw["UploadId"] == uid
    complete_kw = client.calls[2][1]
    assert complete_kw["Bucket"] == "the-bucket"
    assert complete_kw["Key"] == "runs/x/vmlinux"
    assert complete_kw["UploadId"] == uid
    abort_kw = client.calls[3][1]
    assert abort_kw == {"Bucket": "the-bucket", "Key": "runs/x/vmlinux", "UploadId": uid}


class _PaginatorClient:
    """Records list/delete/get kwargs and serves canned pages for the paginated reads."""

    def __init__(self, pages: list[dict[str, object]]) -> None:
        self._pages = pages
        self.paginate_kwargs: dict[str, object] | None = None
        self.delete_kwargs: dict[str, object] | None = None
        self.get_kwargs: dict[str, object] | None = None

    def get_paginator(self, _op: str) -> _PaginatorClient:
        return self

    def paginate(self, **kwargs: object) -> list[dict[str, object]]:
        self.paginate_kwargs = kwargs
        return self._pages

    def delete_object(self, **kwargs: object) -> dict[str, object]:
        self.delete_kwargs = kwargs
        return {}

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.get_kwargs = kwargs
        return {"Body": _StaticBody(b"ranged-bytes")}


def test_list_prefix_returns_keys_and_scopes_to_bucket_and_prefix() -> None:
    client = _PaginatorClient(
        [{"Contents": [{"Key": "p/a"}, {"Key": "p/b"}]}, {"Contents": [{"Key": "p/c"}]}]
    )
    store = ObjectStore(client, "the-bucket")

    assert store.list_prefix("p/") == ["p/a", "p/b", "p/c"]
    assert client.paginate_kwargs == {"Bucket": "the-bucket", "Prefix": "p/"}


def test_list_prefix_empty_when_no_contents() -> None:
    store = ObjectStore(_PaginatorClient([{}]), "the-bucket")
    assert store.list_prefix("p/") == []


def test_list_prefix_maps_client_error_to_infrastructure() -> None:
    class _Raises:
        def get_paginator(self, _op: str) -> object:
            raise ClientError({"Error": {"Code": "boom"}}, "list_objects_v2")

    store = ObjectStore(_Raises(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.list_prefix("p/")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == "object-store list_objects_v2 for 'p/' failed: boom"


def test_delete_version_targets_bound_bucket_key_and_identity() -> None:
    client = _PaginatorClient([])
    store = ObjectStore(client, "the-bucket")
    store.delete_version("t/vmcore/oid/core", "v1")
    assert client.delete_kwargs == {
        "Bucket": "the-bucket",
        "Key": "t/vmcore/oid/core",
        "VersionId": "v1",
    }


def test_delete_version_maps_client_error_to_infrastructure() -> None:
    class _Raises:
        def delete_object(self, **_: object) -> dict[str, object]:
            raise ClientError({"Error": {"Code": "boom"}}, "delete_object")

    store = ObjectStore(_Raises(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.delete_version("k", "null")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == "object-store delete_object for 'k' failed: boom"


def test_get_range_requests_the_inclusive_byte_range() -> None:
    client = _PaginatorClient([])
    store = ObjectStore(client, "the-bucket")

    data = store.get_range("t/vmcore/oid/core", start=10, length=5)

    assert data == b"ranged-bytes"
    assert client.get_kwargs == {
        "Bucket": "the-bucket",
        "Key": "t/vmcore/oid/core",
        "Range": "bytes=10-14",  # end == start + length - 1
    }


def test_get_range_maps_client_error_to_infrastructure() -> None:
    class _Raises:
        def get_object(self, **_: object) -> dict[str, object]:
            raise ClientError({"Error": {"Code": "boom"}}, "get_object")

    store = ObjectStore(_Raises(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.get_range("k", start=0, length=4)
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == "object-store get_range for 'k' failed: boom"
