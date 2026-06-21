"""Behavior and edge tests for the object-store client (ADR-0017).

The MinIO-backed tests use the session ``minio_store`` fixture and gate on Docker
exactly as the db tests do; the pure tests (key validation, etag normalization,
``register_artifact_row``, env config) run without a container.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC
from pathlib import Path
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

from kdive.artifacts.storage import (
    ArtifactStreamRequest,
    ArtifactWriteRequest,
    StoredArtifact,
)
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.store.objectstore import (
    ObjectStore,
    _infrastructure_error,
    _local_stream_error,
    _normalize_etag,
    object_store_from_env,
    register_artifact_row,
)


def test_normalize_etag_strips_surrounding_quotes() -> None:
    assert _normalize_etag('"abc123"') == "abc123"
    assert _normalize_etag("abc123") == "abc123"
    # Only the surrounding double-quotes are stripped; other edge characters are preserved.
    assert _normalize_etag('"Xabc-9X"') == "Xabc-9X"


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
            kwargs["Body"] = body.read()  # ty: ignore[possibly-unbound-attribute]
        self.last_kwargs = kwargs
        return {"ETag": '"stored-etag"'}


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


def test_register_artifact_row_maps_stored_and_owner() -> None:
    stored = StoredArtifact("t/vmcore/oid/core", "etag123", Sensitivity.REDACTED, "vmcore")
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


def test_object_store_from_env_defaults_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("KDIVE_S3_BUCKET", "bucket")
    monkeypatch.delenv("KDIVE_S3_REGION", raising=False)

    store = object_store_from_env()

    assert store._client.meta.region_name == "us-east-1"
    assert store._bucket == "bucket"
    assert store._client.meta.endpoint_url == "http://localhost:9000"


def test_object_store_from_env_uses_configured_region(monkeypatch: pytest.MonkeyPatch) -> None:
    # A configured region is honored verbatim (not collapsed to the default), and the
    # configured endpoint/bucket flow through to the constructed client and store.
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://minio.internal:9000")
    monkeypatch.setenv("KDIVE_S3_BUCKET", "artifacts")
    monkeypatch.setenv("KDIVE_S3_REGION", "eu-west-1")

    store = object_store_from_env()

    assert store._client.meta.region_name == "eu-west-1"
    assert store._client.meta.endpoint_url == "http://minio.internal:9000"
    assert store._bucket == "artifacts"


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


def test_delete_targets_bound_bucket_and_key() -> None:
    client = _PaginatorClient([])
    store = ObjectStore(client, "the-bucket")
    store.delete("t/vmcore/oid/core")
    assert client.delete_kwargs == {"Bucket": "the-bucket", "Key": "t/vmcore/oid/core"}


def test_delete_maps_client_error_to_infrastructure() -> None:
    class _Raises:
        def delete_object(self, **_: object) -> dict[str, object]:
            raise ClientError({"Error": {"Code": "boom"}}, "delete_object")

    store = ObjectStore(_Raises(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.delete("k")
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
