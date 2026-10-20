from __future__ import annotations

import base64
import hashlib
import http.client
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from kdive.artifacts.storage import (
    ConditionalArtifactWriteRequest,
    PresignPutRequest,
    StoredArtifact,
)
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.store.objectstore import ObjectStore


def test_slow_signed_vmcore_put_cannot_pass_retained_fence(minio_store: ObjectStore) -> None:
    execution = uuid4()
    key = f"integration/vmcore-fence/{execution}"
    data = b"vmcore-test-body" * 4096
    checksum = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
    upload = minio_store.presign_put(
        PresignPutRequest(
            key=key,
            sha256=checksum,
            size_bytes=len(data),
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="vmcore",
            expires_in=300,
            if_absent=True,
            metadata={"vmcore-execution": str(execution), "vmcore-kind": "capture"},
        )
    )
    target = urlsplit(upload.url)
    assert target.hostname is not None
    assert "if-none-match" in parse_qs(target.query)["X-Amz-SignedHeaders"][0].split(";")
    connection_type = (
        http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(target.hostname, target.port, timeout=15)
    try:
        connection.putrequest("PUT", target.path + "?" + target.query)
        for name, value in upload.required_headers.items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(len(data)))
        connection.putheader("Expect", "100-continue")
        connection.endheaders()
        assert connection.sock is not None
        with connection.sock.makefile("rb", buffering=0) as interim:
            assert interim.readline(8192) == b"HTTP/1.1 100 Continue\r\n"
            assert interim.readline(8192) == b"\r\n"
        connection.send(data[:1024])
        fence = minio_store.create_if_absent(
            ConditionalArtifactWriteRequest(
                key=key,
                data=b"",
                metadata={"vmcore-execution": str(execution), "vmcore-kind": "tombstone"},
                sensitivity=Sensitivity.SENSITIVE,
                retention_class="vmcore",
            )
        )
        assert isinstance(fence, StoredArtifact)
        connection.send(data[1024:])
        response = connection.getresponse()
        response.read()
        assert response.status == 412
        history = minio_store.capture_exact_versions(key, 10)
        assert history.history_complete
        assert [(entry.version_id, entry.is_delete_marker) for entry in history.targets] == [
            (fence.version_id, False)
        ]
        head = minio_store.head(key)
        assert head is not None and head.version_id == fence.version_id and head.size_bytes == 0
    finally:
        connection.close()
        history = minio_store.capture_exact_versions(key, 10)
        assert history.history_complete, "test created unexpected unbounded history"
        for entry in history.targets:
            minio_store.delete_version(key, entry.version_id)


@pytest.mark.parametrize("condition", [None, "different", "*"])
def test_guest_cannot_strip_signed_condition(
    minio_store: ObjectStore, condition: str | None
) -> None:
    key = f"integration/vmcore-signed-condition/{uuid4()}"
    body = b"test"
    checksum = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
    upload = minio_store.presign_put(
        PresignPutRequest(
            key=key,
            sha256=checksum,
            size_bytes=len(body),
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="vmcore",
            expires_in=300,
            if_absent=True,
        )
    )
    target = urlsplit(upload.url)
    assert target.hostname is not None
    assert "if-none-match" in parse_qs(target.query)["X-Amz-SignedHeaders"][0].split(";")
    connection_type = (
        http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(target.hostname, target.port, timeout=15)
    headers = dict(upload.required_headers)
    headers.pop("if-none-match")
    if condition is not None:
        headers["if-none-match"] = condition
    try:
        connection.request("PUT", target.path + "?" + target.query, body=body, headers=headers)
        response = connection.getresponse()
        response.read()
        assert response.status == (200 if condition == "*" else 403)
        head = minio_store.head(key)
        assert (head is not None) is (condition == "*")
        if head is not None:
            assert head.size_bytes == len(body)
    finally:
        connection.close()
        for version in minio_store.capture_exact_versions(key, 10).targets:
            minio_store.delete_version(key, version.version_id)


@pytest.mark.parametrize("signature", ["valid", "invalid", "absent"])
def test_vmcore_destination_requires_authentication(
    minio_store: ObjectStore, signature: str
) -> None:
    key = f"integration/vmcore-auth-control/{uuid4()}"
    data = b"auth-control"
    upload = minio_store.presign_put(
        PresignPutRequest(
            key=key,
            sha256=base64.b64encode(hashlib.sha256(data).digest()).decode("ascii"),
            size_bytes=len(data),
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="vmcore",
            expires_in=300,
            if_absent=True,
        )
    )
    target = urlsplit(upload.url)
    assert target.hostname is not None
    query = parse_qs(target.query)
    assert "if-none-match" in query["X-Amz-SignedHeaders"][0].split(";")
    path = target.path + "?" + target.query
    if signature == "invalid":
        path = path.replace(query["X-Amz-Signature"][0], "0" * 64)
    elif signature == "absent":
        path = target.path
    connection = http.client.HTTPConnection(target.hostname, target.port, timeout=15)
    try:
        connection.request("PUT", path, body=data, headers=upload.required_headers)
        response = connection.getresponse()
        response.read()
        assert response.status == (200 if signature == "valid" else 403)
    finally:
        connection.close()
        for version in minio_store.capture_exact_versions(key, 10).targets:
            minio_store.delete_version(key, version.version_id)
