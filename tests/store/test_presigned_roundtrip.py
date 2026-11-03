"""Real-MinIO presigned-PUT checksum enforcement (ADR-0048 §2, §7).

Proves the load-bearing assumption: MinIO rejects a presigned PUT whose body checksum
disagrees with the signed ``x-amz-checksum-sha256``, and accepts a matching upload. Runs
against the ``minio_store`` testcontainer (Docker-gated; skips without Docker).
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from kdive.artifacts.storage import (
    ArtifactWriteRequest,
    PresignPutRequest,
    owner_prefix,
)
from kdive.artifacts.vmcore_retention import signed_get_deadline
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory


def _b64_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def test_owner_prefix_builds_trailing_slash_key() -> None:
    assert owner_prefix("t", "runs", "r1") == "t/runs/r1/"


@pytest.mark.parametrize(
    ("tenant", "kind", "object_id", "expected_label"),
    [
        ("", "runs", "r1", "tenant"),
        ("t", "", "r1", "kind"),
        ("t", "runs", "", "object_id"),
    ],
)
def test_owner_prefix_empty_component_names_its_label(
    tenant: str, kind: str, object_id: str, expected_label: str
) -> None:
    # An empty component is rejected with a configuration error that names WHICH component failed;
    # the label must match the position (tenant/kind/object_id), not a neighbor's.
    with pytest.raises(CategorizedError) as exc:
        owner_prefix(tenant, kind, object_id)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == f"artifact key component '{expected_label}' must not be empty"


@pytest.mark.parametrize(
    ("tenant", "kind", "object_id", "expected_label"),
    [
        ("a/b", "runs", "r1", "tenant"),
        ("t", "ru/ns", "r1", "kind"),
        ("t", "runs", "r/1", "object_id"),
    ],
)
def test_owner_prefix_illegal_char_component_names_its_label(
    tenant: str, kind: str, object_id: str, expected_label: str
) -> None:
    # A component carrying a path separator is rejected with the offending component's label, so a
    # traversal-bearing value cannot be mislabeled (which would misdirect the operator's fix).
    with pytest.raises(CategorizedError) as exc:
        owner_prefix(tenant, kind, object_id)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value).startswith(f"artifact key component '{expected_label}' has an illegal")


def test_presigned_get_fetches_the_published_object(minio_store, key_ns: str) -> None:
    """The in-target seam's pull half: publish an object, then fetch it by presigned GET.

    Proves the publish→presign_get→in-guest-pull path the artifact channel relies on
    (ADR-0078): a bounded GET URL retrieves exactly the published bytes from real MinIO.
    """
    payload = b"published-kernel-bytes"
    stored = minio_store.put_artifact(
        ArtifactWriteRequest(
            tenant=key_ns,
            owner_kind="runs",
            owner_id="r1",
            name="kernel",
            data=payload,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
        )
    )
    url = minio_store.presign_get(stored.key, expires_in=300)
    resp = httpx.get(url)
    assert resp.status_code == 200
    assert resp.content == payload


def test_presigned_vmcore_get_has_a_deadline_and_reads_back(minio_store, key_ns: str) -> None:
    # The vmcore read path protects the URL by its signed deadline before handing it out; that
    # failed on every SigV2 URL the stack minted (#3313).
    payload = b"vmcore-bytes"
    stored = minio_store.put_artifact(
        ArtifactWriteRequest(
            tenant=key_ns,
            owner_kind="runs",
            owner_id="r1",
            name="vmcore-kdump-redacted",
            data=payload,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="vmcore",
        )
    )
    before = datetime.now(UTC)
    url = minio_store.presign_get(stored.key, expires_in=300, version_id=stored.version_id)

    deadline = signed_get_deadline(url)

    assert before - timedelta(seconds=1) <= deadline - timedelta(seconds=300) <= datetime.now(UTC)
    resp = httpx.get(url)
    assert resp.status_code == 200
    assert resp.content == payload


def test_conditional_presigned_put_requires_the_signed_if_none_match(
    minio_store, key_ns: str
) -> None:
    # The vmcore write fence: a client that drops If-None-Match fails the signature, so it cannot
    # turn a create-only upload into an overwrite (#3313).
    payload = b"fenced-bytes"
    presigned = minio_store.presign_put(
        PresignPutRequest(
            key=f"{key_ns}/runs/r1/vmcore-raw",
            sha256=_b64_sha256(payload),
            size_bytes=len(payload),
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="vmcore",
            expires_in=300,
            if_absent=True,
        )
    )
    headers = dict(presigned.required_headers)
    assert headers.pop("if-none-match") == "*"

    resp = httpx.put(presigned.url, content=payload, headers=headers)

    assert resp.status_code == 403
    assert "SignatureDoesNotMatch" in resp.text


def test_presigned_put_rejects_checksum_mismatch(minio_store, key_ns: str) -> None:
    payload = b"correct-bytes"
    wrong = _b64_sha256(b"different")
    key = f"{key_ns}/runs/r1/kernel"
    presigned = minio_store.presign_put(
        PresignPutRequest(
            key=key,
            sha256=wrong,
            size_bytes=len(payload),
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
            expires_in=300,
        )
    )
    resp = httpx.put(presigned.url, content=payload, headers=presigned.required_headers)
    assert resp.status_code >= 400  # the signed checksum disagrees with the body


def test_presigned_put_accepts_matching_upload(minio_store, key_ns: str) -> None:
    payload = b"correct-bytes"
    checksum = _b64_sha256(payload)
    key = f"{key_ns}/runs/r1/kernel"
    presigned = minio_store.presign_put(
        PresignPutRequest(
            key=key,
            sha256=checksum,
            size_bytes=len(payload),
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
            expires_in=300,
        )
    )
    resp = httpx.put(presigned.url, content=payload, headers=presigned.required_headers)
    assert resp.status_code < 300
    head = minio_store.head(key)
    assert head is not None
    assert head.checksum_sha256 == checksum
    assert head.size_bytes == len(payload)
