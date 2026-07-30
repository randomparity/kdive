"""The store boundary converts a malformed **successful** reply into a ``CategorizedError`` (#1685).

Every field ``ObjectStore`` reads out of a reply is one boto3 parsed against the S3 service model,
so a store that implements the API always supplies it. The failure these tests pin is the other
case: a store — a proxy, a partial S3 implementation, a mock wired into a deployment — that returns
200 with a field missing or of the wrong type. Left alone that raises a bare ``KeyError`` (or, past
the subscript, an ``AttributeError`` out of ``_normalize_etag``), which is *not* the
``CategorizedError`` this module's callers handle; the ADR-0455 upload orphan sweep catches
``CategorizedError`` alone for its per-key and per-root faults and deliberately lets anything else
abort the whole pass.

Both halves of the contract are asserted here — that the failure is a ``CategorizedError``, and that
its message and details name the store call, the bucket, the subject, and the offending *field*,
because "object-store head_object failed" tells an operator nothing they can act on. The
sweep-level consequence — that the pass survives it and finishes its remaining keys — is pinned in
``tests/reconciler/test_upload_orphan_sweep_malformed_reply.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.store.objectstore import ObjectStore
from tests.clock import STORE_MTIME

#: A well-formed ``head_object`` reply, from which each test removes or corrupts one field.
_HEAD_REPLY: dict[str, Any] = {
    "ContentLength": 7,
    "ETag": '"abc"',
    "LastModified": STORE_MTIME,
    "Metadata": {"sensitivity": "redacted", "retention-class": "vmcore"},
}

#: A well-formed ``list_objects_v2`` entry, same treatment.
_LISTING_ENTRY: dict[str, Any] = {"Key": "local/runs/oid/vmcore", "LastModified": STORE_MTIME}

#: The fields ``head`` requires, paired with a value of the wrong type for each.
_HEAD_FIELDS = [
    ("ContentLength", "7", "str", "int"),
    ("ETag", 12, "int", "str"),
    ("LastModified", "2026-07-29T00:00:00Z", "str", "datetime"),
]

#: The fields a listing entry requires, same shape.
_LISTING_FIELDS = [
    ("Key", 5, "int", "str"),
    ("LastModified", "2026-07-29T00:00:00Z", "str", "datetime"),
]


class _CannedHeadClient:
    """A stub whose ``head_object`` succeeds and returns exactly the reply it was handed."""

    def __init__(self, reply: dict[str, Any]) -> None:
        self._reply = reply

    def head_object(self, **_kwargs: object) -> dict[str, Any]:
        return self._reply


class _CannedPagesClient:
    """A stub paginator serving exactly the ``list_objects_v2`` pages it was handed."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def get_paginator(self, _op: str) -> _CannedPagesClient:
        return self

    def paginate(self, **_kwargs: object) -> list[dict[str, Any]]:
        return self._pages


def _without(reply: dict[str, Any], field: str) -> dict[str, Any]:
    return {name: value for name, value in reply.items() if name != field}


@pytest.mark.parametrize("field", [name for name, _v, _got, _want in _HEAD_FIELDS])
def test_head_omitting_a_required_field_raises_a_categorized_error(field: str) -> None:
    """A reply missing any one of ``head``'s three required fields fails the same, actionable way.

    Parametrized over all three rather than pinning ``LastModified`` alone — the field #1575 made
    load-bearing for the sweep — because guarding the one field a caller happens to have reached
    would leave the next caller to rediscover the same defect on ``ETag`` or ``ContentLength``.
    """
    store = ObjectStore(_CannedHeadClient(_without(_HEAD_REPLY, field)), "the-bucket")

    with pytest.raises(CategorizedError) as excinfo:
        store.head("local/runs/oid/vmcore")

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == (
        f"object-store head_object for 'local/runs/oid/vmcore' in bucket 'the-bucket' omitted the "
        f"required {field!r}; the endpoint is not returning S3-compatible head_object replies"
    )
    assert excinfo.value.details == {
        "op": "head_object",
        "bucket": "the-bucket",
        "key": "local/runs/oid/vmcore",
        "field": field,
    }


@pytest.mark.parametrize(("field", "value", "got", "want"), _HEAD_FIELDS)
def test_head_returning_a_required_field_as_the_wrong_type_raises_a_categorized_error(
    field: str, value: object, got: str, want: str
) -> None:
    """An ill-typed field is a malformed reply too, and it is the half a presence check misses.

    A ``str`` where a ``datetime`` belongs does not raise at the subscript: it raises later, in a
    caller (psycopg binding a ``timestamptz[]``, ``_normalize_etag`` calling ``.strip``), where the
    exception no longer names the store or the field. Checking the type at the boundary is what
    keeps the diagnosis where the fault is.
    """
    store = ObjectStore(_CannedHeadClient({**_HEAD_REPLY, field: value}), "the-bucket")

    with pytest.raises(CategorizedError) as excinfo:
        store.head("k")

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == (
        f"object-store head_object for 'k' in bucket 'the-bucket' returned {field!r} as {got}, "
        f"not {want}; the endpoint is not returning S3-compatible head_object replies"
    )
    assert excinfo.value.details["field"] == field


def test_head_still_returns_a_result_when_only_the_optional_fields_are_absent() -> None:
    """The contract is required-fields-only: an absent ``ChecksumSHA256`` or ``Metadata`` is normal.

    This is the over-rejection guard. A boundary check that treated every field as required would
    fail a perfectly good reply — objects written before checksums were requested carry no
    ``ChecksumSHA256``, and a store may omit ``Metadata`` entirely for an object with none — so the
    line between required and optional is itself worth pinning.
    """
    reply = {"ContentLength": 7, "ETag": '"abc"', "LastModified": STORE_MTIME}

    head = ObjectStore(_CannedHeadClient(reply), "the-bucket").head("k")

    assert head is not None
    assert (head.size_bytes, head.etag, head.last_modified) == (7, "abc", STORE_MTIME)
    assert head.checksum_sha256 is None
    assert head.sensitivity is None
    assert head.content_encoding is None


@pytest.mark.parametrize(
    ("field", "value", "got", "want"),
    [("Metadata", ["sensitivity"], "list", "dict"), ("ChecksumSHA256", 7, "int", "str")],
)
def test_head_rejects_a_present_but_ill_typed_optional_field(
    field: str, value: object, got: str, want: str
) -> None:
    """An optional field is exempt from being *present*, not from being the right type.

    ``Metadata`` is the one that matters and the reason this arm exists. It is read as a mapping
    two lines later, so a list or a string there raises ``TypeError`` — a class neither ``head``'s
    own ``except (KeyError, ValueError)`` nor the orphan sweep's per-key handler catches. That is
    the same escape a missing required field caused, arriving by way of an optional one, so leaving
    it would have made the contract's "every field a read returns" claim false.
    """
    store = ObjectStore(_CannedHeadClient({**_HEAD_REPLY, field: value}), "the-bucket")

    with pytest.raises(CategorizedError) as excinfo:
        store.head("k")

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == (
        f"object-store head_object for 'k' in bucket 'the-bucket' returned {field!r} as {got}, "
        f"not {want}; the endpoint is not returning S3-compatible head_object replies"
    )


@pytest.mark.parametrize("field", [name for name, _v, _got, _want in _LISTING_FIELDS])
def test_a_paged_listing_entry_omitting_a_required_field_raises_a_categorized_error(
    field: str,
) -> None:
    """``iter_prefix_pages_with_mtime`` gets the same treatment as ``head``, for the same reason.

    It is the sweep's *other* store read, and its fault handler (``_next_page_or_fault``) also
    catches ``CategorizedError`` alone. A bare ``KeyError`` from an entry would escape it and end
    the pass — and end it worse than the ``head`` case does, because a listing fault abandons the
    root, leaving the sibling root unswept as well.

    The failure names the **prefix**, not a key: an entry whose ``Key`` is what is missing has no
    key to be named by.
    """
    pages = [{"Contents": [_without(_LISTING_ENTRY, field)]}]
    store = ObjectStore(_CannedPagesClient(pages), "the-bucket")

    with pytest.raises(CategorizedError) as excinfo:
        list(store.iter_prefix_pages_with_mtime("local/runs/"))

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == (
        f"object-store list_objects_v2 for 'local/runs/' in bucket 'the-bucket' omitted the "
        f"required {field!r}; the endpoint is not returning S3-compatible list_objects_v2 replies"
    )
    assert excinfo.value.details == {
        "op": "list_objects_v2",
        "bucket": "the-bucket",
        "key": "local/runs/",
        "field": field,
    }


@pytest.mark.parametrize(("field", "value", "got", "want"), _LISTING_FIELDS)
def test_a_paged_listing_entry_with_a_wrong_typed_field_raises_a_categorized_error(
    field: str, value: object, got: str, want: str
) -> None:
    store = ObjectStore(
        _CannedPagesClient([{"Contents": [{**_LISTING_ENTRY, field: value}]}]), "the-bucket"
    )

    with pytest.raises(CategorizedError) as excinfo:
        list(store.iter_prefix_pages_with_mtime("local/runs/"))

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == (
        f"object-store list_objects_v2 for 'local/runs/' in bucket 'the-bucket' returned {field!r} "
        f"as {got}, not {want}; the endpoint is not returning S3-compatible list_objects_v2 replies"
    )


def test_a_malformed_entry_on_a_later_page_still_faults_from_the_iterator() -> None:
    """The fault surfaces at the page that carried it, not at the call.

    ``iter_prefix_pages_with_mtime`` is a generator whose whole purpose is that a caller acts page
    by page (ADR-0498), and the sweep's mid-root fault handling depends on a fault being able to
    arrive after earlier pages have already been acted on. A malformed entry must behave like the
    listing errors already do: the good page is delivered, and only the advance past it raises.
    """
    good = {"Key": "local/runs/a", "LastModified": STORE_MTIME}
    pages = [{"Contents": [good]}, {"Contents": [_without(_LISTING_ENTRY, "LastModified")]}]
    store = ObjectStore(_CannedPagesClient(pages), "the-bucket")

    walk = store.iter_prefix_pages_with_mtime("local/runs/")

    first = next(walk)
    assert [listed.key for listed in first] == ["local/runs/a"]
    with pytest.raises(CategorizedError):
        next(walk)


def test_list_prefix_requires_only_the_key_it_returns() -> None:
    """The flat listing needs ``Key`` and nothing else, so that is all it may reject a reply for.

    ``list_prefix`` returns keys alone; requiring ``LastModified`` here would fail replies it has
    no use for. The contract is per read, not one field set for the whole module.
    """
    pages = [{"Contents": [{"Key": "p/a"}, {"Key": "p/b"}]}]
    assert ObjectStore(_CannedPagesClient(pages), "the-bucket").list_prefix("p/") == ["p/a", "p/b"]

    keyless = ObjectStore(_CannedPagesClient([{"Contents": [{"Size": 1}]}]), "the-bucket")
    with pytest.raises(CategorizedError) as excinfo:
        keyless.list_prefix("p/")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert excinfo.value.details["field"] == "Key"
