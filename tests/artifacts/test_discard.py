"""Tests for the compensating delete of unregistered objects (ADR-0519, #1725).

``discard_unregistered_objects`` is the abort path of every worker handler that PUTs its object
outside the advisory lock. Its contract is narrow and entirely about not making a bad situation
worse: delete only what this attempt still owns, and never raise into a caller that is already
failing. The two fences — the row re-probe and the etag comparison — exist because the lock is
released by the time it runs, so a peer attempt of the same job can have claimed the key.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

from kdive.artifacts.discard import discard_unregistered_objects
from kdive.artifacts.storage import HeadResult, StoredArtifact
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.store.objectstore import ObjectStore
from tests.clock import STORE_MTIME


def _written(key: str, etag: str) -> StoredArtifact:
    return StoredArtifact(key, etag, Sensitivity.REDACTED, "console", version_id="test-version")


class _RecordingStore:
    """Serves ``head`` from ``etags`` and records deletes; faults on the keys in ``fails_on``."""

    def __init__(
        self,
        etags: dict[str, str] | None = None,
        fails_on: frozenset[str] = frozenset(),
    ) -> None:
        self.etags = {} if etags is None else dict(etags)
        self.attempted: list[str] = []
        self.deleted: list[str] = []
        self._fails_on = fails_on

    def head(self, key: str) -> HeadResult | None:
        if key not in self.etags:
            return None
        return HeadResult(
            size_bytes=1,
            checksum_sha256=None,
            etag=self.etags[key],
            last_modified=STORE_MTIME,
            version_id="test-version",
        )

    def delete(self, key: str) -> None:
        self.attempted.append(key)
        if key in self._fails_on:
            raise CategorizedError(
                "delete_object failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        self.deleted.append(key)
        self.etags.pop(key, None)


def _discard(
    store: _RecordingStore,
    written: list[StoredArtifact],
    *,
    registered: frozenset[str] = frozenset(),
) -> None:
    async def _still_unregistered(key: str) -> bool:
        return key not in registered

    asyncio.run(
        discard_unregistered_objects(
            cast(ObjectStore, store), written, still_unregistered=_still_unregistered
        )
    )


def test_an_object_this_attempt_still_owns_is_deleted() -> None:
    store = _RecordingStore({"a/1": "etag-1", "a/2": "etag-2"})
    _discard(store, [_written("a/1", "etag-1"), _written("a/2", "etag-2")])
    assert store.deleted == ["a/1", "a/2"]


def test_no_keys_touches_the_store_at_all() -> None:
    store = _RecordingStore({"a/1": "etag-1"})
    _discard(store, [])
    assert store.attempted == []
    assert store.etags == {"a/1": "etag-1"}


def test_a_key_a_peer_row_claims_is_left_alone() -> None:
    """The row fence: a row that appeared after the lock was released owns its object.

    Deleting here would leave a committed ``artifacts`` row pointing at nothing, which the
    row-driven reclaim would never notice — strictly worse than the orphan it prevents.
    """
    store = _RecordingStore({"a/1": "etag-1", "a/2": "etag-2"})
    _discard(
        store,
        [_written("a/1", "etag-1"), _written("a/2", "etag-2")],
        registered=frozenset({"a/1"}),
    )
    assert store.deleted == ["a/2"]  # only the genuinely unclaimed key
    assert "a/1" in store.etags  # the claimed object survived


def test_an_object_another_writer_replaced_is_left_alone() -> None:
    """The etag fence: different bytes under the key mean another attempt owns what is there."""
    store = _RecordingStore({"a/1": "etag-peer"})
    _discard(store, [_written("a/1", "etag-mine")])
    assert store.attempted == []
    assert store.etags == {"a/1": "etag-peer"}


def test_an_already_absent_object_is_not_deleted_again() -> None:
    store = _RecordingStore({})
    _discard(store, [_written("a/1", "etag-1")])
    assert store.attempted == []


def test_a_faulting_key_does_not_strand_the_rest(caplog) -> None:
    """One unreachable object must not leave its siblings orphaned as well.

    Each key is an independent object with no row; abandoning the loop on the first fault would
    turn one permanent orphan into several.
    """
    store = _RecordingStore(
        {"a/1": "etag-1", "a/2": "etag-2", "a/3": "etag-3"}, fails_on=frozenset({"a/2"})
    )
    with caplog.at_level(logging.WARNING, logger="kdive.artifacts.discard"):
        _discard(
            store,
            [_written("a/1", "etag-1"), _written("a/2", "etag-2"), _written("a/3", "etag-3")],
        )
    assert store.attempted == ["a/1", "a/2", "a/3"]  # the fault did not end the loop
    assert store.deleted == ["a/1", "a/3"]
    # The orphan the fault leaves behind is named in the log, since nothing else will find it.
    assert any("a/2" in record.getMessage() for record in caplog.records)


def test_a_fault_never_raises_into_the_caller() -> None:
    """The caller is already on an abort path; its own outcome is the result that matters."""
    store = _RecordingStore({"a/1": "etag-1"}, fails_on=frozenset({"a/1"}))
    _discard(store, [_written("a/1", "etag-1")])  # would raise if the fault were propagated
    assert store.attempted == ["a/1"]
    assert store.deleted == []
