"""Tests for the ingestion-lane object-store methods (ADR-0048)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from kdive.artifacts.storage import (
    HeadResult,
    ObjectListing,
    PresignedUpload,
    PresignPutRequest,
)
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.store.objectstore import (
    _LIST_PAGE_SIZE,
    ObjectStore,
    artifact_key,
    owner_prefix,
)
from tests.clock import STORE_MTIME


class _HeadClient:
    def __init__(self, response: dict[str, object] | Exception) -> None:
        self._response = response

    def head_object(self, **_kwargs: object) -> dict[str, object]:
        assert _kwargs.get("ChecksumMode") == "ENABLED"
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _not_found() -> ClientError:
    return ClientError(
        {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject"
    )


def _forbidden() -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
        "HeadObject",
    )


def test_head_returns_size_checksum_and_etag() -> None:
    store = ObjectStore(
        _HeadClient(
            {
                "ContentLength": 42,
                "ChecksumSHA256": "Zm9vYmFy",
                "ETag": '"abc123"',
                "LastModified": STORE_MTIME,
                "VersionId": "head-version-1",
            }
        ),
        "bucket",
    )
    result = store.head("t/runs/r1/kernel")
    assert result == HeadResult(
        size_bytes=42,
        checksum_sha256="Zm9vYmFy",
        etag="abc123",
        last_modified=STORE_MTIME,
        version_id="head-version-1",
    )


def test_head_missing_object_returns_none() -> None:
    store = ObjectStore(_HeadClient(_not_found()), "bucket")
    assert store.head("t/runs/r1/kernel") is None


def test_head_without_checksum_metadata_yields_none_checksum() -> None:
    store = ObjectStore(
        _HeadClient(
            {
                "ContentLength": 7,
                "ETag": '"e"',
                "LastModified": STORE_MTIME,
                "VersionId": "null",
            }
        ),
        "bucket",
    )
    result = store.head("t/runs/r1/kernel")
    assert result is not None and result.checksum_sha256 is None
    assert result.version_id == "null"


def test_head_maps_transport_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_HeadClient(EndpointConnectionError(endpoint_url="http://x")), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.head("t/runs/r1/kernel")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_head_non_404_client_error_raises_infrastructure_failure() -> None:
    store = ObjectStore(_HeadClient(_forbidden()), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.head("t/runs/r1/kernel")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_artifact_key_and_owner_prefix_match_layout() -> None:
    assert artifact_key("local", "runs", "r1", "kernel") == "local/runs/r1/kernel"
    assert owner_prefix("local", "runs", "r1") == "local/runs/r1/"


class _RangeClient:
    def get_object(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["Range"] == "bytes=0-3"

        class _Body:
            def read(self) -> bytes:
                return b"\x7fELF"

        return {"Body": _Body()}


def test_get_range_requests_byte_range() -> None:
    store = ObjectStore(_RangeClient(), "bucket")
    assert store.get_range("t/runs/r1/vmlinux", start=0, length=4) == b"\x7fELF"


class _PresignClient:
    def __init__(self) -> None:
        self.params: dict[str, object] | None = None

    def generate_presigned_url(
        self, op: str, *, Params: dict[str, object], ExpiresIn: int, HttpMethod: str
    ) -> str:
        assert op == "put_object" and HttpMethod == "PUT"
        self.params = Params
        return f"https://store/put?exp={ExpiresIn}"


def test_presign_put_signs_checksum_and_metadata() -> None:
    client = _PresignClient()
    store = ObjectStore(client, "bucket")
    out = store.presign_put(
        PresignPutRequest(
            key="local/runs/r1/kernel",
            sha256="Zm9vYmFy",
            size_bytes=10,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
            expires_in=900,
        )
    )
    assert isinstance(out, PresignedUpload)
    assert out.url == "https://store/put?exp=900"
    assert client.params is not None
    assert client.params["ChecksumSHA256"] == "Zm9vYmFy"
    assert client.params["Metadata"] == {
        "sensitivity": "sensitive",
        "retention-class": "build",
    }
    assert out.required_headers["x-amz-checksum-sha256"] == "Zm9vYmFy"
    assert out.required_headers["x-amz-meta-sensitivity"] == "sensitive"
    assert out.required_headers["x-amz-meta-retention-class"] == "build"


class _FailingGetClient:
    def get_object(self, **_kwargs: object) -> dict[str, object]:
        raise EndpointConnectionError(endpoint_url="http://x")


def test_get_range_maps_transport_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_FailingGetClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.get_range("t/runs/r1/vmlinux", start=0, length=4)
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


class _FailingPresignClient:
    def generate_presigned_url(self, *_a: object, **_k: object) -> str:
        raise EndpointConnectionError(endpoint_url="http://x")


def test_presign_put_maps_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_FailingPresignClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.presign_put(
            PresignPutRequest(
                key="local/runs/r1/kernel",
                sha256="Zm9v",
                size_bytes=10,
                sensitivity=Sensitivity.SENSITIVE,
                retention_class="build",
                expires_in=900,
            )
        )
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_owner_prefix_rejects_invalid_component() -> None:
    with pytest.raises(CategorizedError):
        owner_prefix("local", "runs", "bad/id")


class _ListClient:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self._pages = pages
        self.deleted: list[str] = []

    def get_paginator(self, op: str) -> object:
        assert op == "list_objects_v2"
        pages = self._pages

        class _Paginator:
            def paginate(self, **_kwargs: object):
                yield from pages

        return _Paginator()

    def delete_object(self, **kwargs: object) -> dict[str, object]:
        self.deleted.append(str(kwargs["Key"]))
        return {}


def test_list_prefix_flattens_pages() -> None:
    client = _ListClient(
        [
            {"Contents": [{"Key": "p/a"}, {"Key": "p/b"}]},
            {"Contents": [{"Key": "p/c"}]},
            {},  # empty page (no Contents) tolerated
        ]
    )
    store = ObjectStore(client, "bucket")
    assert store.list_prefix("p/") == ["p/a", "p/b", "p/c"]


def test_delete_calls_delete_object() -> None:
    client = _ListClient([])
    store = ObjectStore(client, "bucket")
    store.delete("p/a")
    assert client.deleted == ["p/a"]


class _FailingListClient:
    def get_paginator(self, _op: str) -> object:
        class _Paginator:
            def paginate(self, **_kwargs: object):
                raise EndpointConnectionError(endpoint_url="http://x")
                yield  # make this a generator

        return _Paginator()


def test_list_prefix_maps_transport_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_FailingListClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.list_prefix("p/")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


class _FailingDeleteClient:
    def delete_object(self, **_kwargs: object) -> dict[str, object]:
        raise EndpointConnectionError(endpoint_url="http://x")


def test_delete_maps_transport_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_FailingDeleteClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.delete("p/a")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


class _MtimeListClient:
    """A paginating list client recording the ``Prefix`` and ``PageSize`` of each paginate call."""

    def __init__(self, pages: list[dict[str, object]]) -> None:
        self._pages = pages
        self.prefixes: list[str] = []
        self.page_sizes: list[object] = []

    def get_paginator(self, op: str) -> object:
        assert op == "list_objects_v2"
        pages, prefixes, page_sizes = self._pages, self.prefixes, self.page_sizes

        class _Paginator:
            def paginate(self, **kwargs: object):
                prefixes.append(str(kwargs["Prefix"]))
                config = cast(dict[str, object], kwargs.get("PaginationConfig", {}))
                page_sizes.append(config.get("PageSize"))
                yield from pages

        return _Paginator()


class _FaultAfterOnePageClient:
    """Yields one good page, then raises — the mid-listing fault a flat listing could not have."""

    def get_paginator(self, _op: str) -> object:
        class _Paginator:
            def paginate(self, **_kwargs: object):
                yield {
                    "Contents": [
                        {
                            "Key": "local/runs/r1/kernel",
                            "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
                        }
                    ]
                }
                raise EndpointConnectionError(endpoint_url="http://x")

        return _Paginator()


def _mtime_pages() -> list[dict[str, object]]:
    return [
        {
            "Contents": [
                {"Key": "local/runs/r1/kernel", "LastModified": datetime(2026, 1, 1, tzinfo=UTC)},
            ]
        },
        {
            "Contents": [
                {"Key": "local/runs/r1/stray", "LastModified": datetime(2026, 1, 2, tzinfo=UTC)},
            ]
        },
        {},  # empty page (no Contents) tolerated
    ]


def test_iter_prefix_pages_with_mtime_yields_a_page_at_a_time_in_store_order() -> None:
    """ADR-0498 §1: the paged primitive hands each ``list_objects_v2`` page over as it arrives.

    The page boundaries are what the upload orphan sweep's memory and parameter-width bound is made
    of, so they have to survive the call rather than be flattened away — a method that returned
    ``[[everything]]`` would satisfy an "is it an iterator" check and bound nothing. The empty final
    page is yielded too: a caller counting pages must not read a prefix's last (or only) reply as no
    request having been made.
    """
    client = _MtimeListClient(_mtime_pages())
    store = ObjectStore(client, "bucket")
    pages = [[listing.key for listing in page] for page in store.iter_prefix_pages_with_mtime("p/")]
    assert pages == [["local/runs/r1/kernel"], ["local/runs/r1/stray"], []]
    assert client.prefixes == ["p/"]
    # The bound is the store's own constant, not whatever boto3 defaults to next.
    assert client.page_sizes == [_LIST_PAGE_SIZE]


def test_iter_prefix_pages_with_mtime_maps_a_mid_listing_error_from_the_iterator() -> None:
    """A fault *after* a delivered page still surfaces as the typed store failure.

    The mapping has to live in the generator body rather than around the call, because with the flat
    listing the whole enumeration happened inside the call and now it does not. So the case that
    distinguishes the two placements is a fault the caller reaches **after** consuming a page — a
    `try` wrapped only around the setup would let a raw ``EndpointConnectionError`` escape from the
    second ``next``, and the sweep would abort the pass instead of counting a root fault
    (ADR-0498 §3). The first page is asserted to arrive intact so the fault really is the second
    round trip and not the first.
    """
    store = ObjectStore(_FaultAfterOnePageClient(), "bucket")
    pages = store.iter_prefix_pages_with_mtime("local/runs/")
    assert [listing.key for listing in next(pages)] == ["local/runs/r1/kernel"]
    with pytest.raises(CategorizedError) as excinfo:
        next(pages)
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_list_prefix_with_mtime_flattens_pages_and_scopes_to_the_prefix() -> None:
    # ADR-0455: the orphan sweeps compare an unreferenced object's store mtime against a grace in
    # Postgres, so the listing must carry LastModified and honour an arbitrary caller prefix.
    client = _MtimeListClient(_mtime_pages())
    store = ObjectStore(client, "bucket")
    assert store.list_prefix_with_mtime("local/runs/") == [
        ObjectListing(key="local/runs/r1/kernel", last_modified=datetime(2026, 1, 1, tzinfo=UTC)),
        ObjectListing(key="local/runs/r1/stray", last_modified=datetime(2026, 1, 2, tzinfo=UTC)),
    ]
    assert client.prefixes == ["local/runs/"]


def test_list_image_objects_delegates_to_the_image_prefix() -> None:
    # The ImageSweepStore port stays prefix-free on purpose: an image sweep must not gain the
    # authority to list an arbitrary prefix, so the prefix is bound here rather than passed in.
    client = _MtimeListClient(_mtime_pages())
    store = ObjectStore(client, "bucket")
    assert len(store.list_image_objects()) == 2
    assert client.prefixes == ["images/"]


def test_list_prefix_with_mtime_maps_transport_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_FailingListClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.list_prefix_with_mtime("local/runs/")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
