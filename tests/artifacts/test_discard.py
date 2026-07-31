"""Tests for the compensating delete of unregistered objects (ADR-0519, #1725).

``discard_unregistered_objects`` is the abort path of every worker handler that PUTs its object
outside the advisory lock. Its contract is narrow and entirely about not making a bad situation
worse: delete every key it is given, and never raise into a caller that is already failing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

from kdive.artifacts.discard import discard_unregistered_objects
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.store.objectstore import ObjectStore


class _RecordingStore:
    """Records deleted keys; optionally faults on the keys named in ``fails_on``."""

    def __init__(self, fails_on: frozenset[str] = frozenset()) -> None:
        self.attempted: list[str] = []
        self.deleted: list[str] = []
        self._fails_on = fails_on

    def delete(self, key: str) -> None:
        self.attempted.append(key)
        if key in self._fails_on:
            raise CategorizedError(
                "delete_object failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        self.deleted.append(key)


def _discard(store: _RecordingStore, keys: list[str]) -> None:
    asyncio.run(discard_unregistered_objects(cast(ObjectStore, store), keys))


def test_every_key_is_deleted_in_the_order_given() -> None:
    store = _RecordingStore()
    _discard(store, ["a/1", "a/2", "a/3"])
    assert store.deleted == ["a/1", "a/2", "a/3"]


def test_no_keys_touches_the_store_at_all() -> None:
    store = _RecordingStore()
    _discard(store, [])
    assert store.attempted == []


def test_a_faulting_key_does_not_strand_the_rest(caplog) -> None:
    """One unreachable object must not leave its siblings orphaned as well.

    Each key is an independent object with no row; abandoning the loop on the first fault would
    turn one permanent orphan into several.
    """
    store = _RecordingStore(fails_on=frozenset({"a/2"}))
    with caplog.at_level(logging.WARNING, logger="kdive.artifacts.discard"):
        _discard(store, ["a/1", "a/2", "a/3"])
    assert store.attempted == ["a/1", "a/2", "a/3"]  # the fault did not end the loop
    assert store.deleted == ["a/1", "a/3"]
    # The orphan the fault leaves behind is named in the log, since nothing else will find it.
    assert any("a/2" in record.getMessage() for record in caplog.records)


def test_a_fault_never_raises_into_the_caller() -> None:
    """The caller is already on an abort path; its own outcome is the result that matters."""
    store = _RecordingStore(fails_on=frozenset({"a/1"}))
    _discard(store, ["a/1"])  # would raise CategorizedError if the fault were propagated
    assert store.attempted == ["a/1"]
    assert store.deleted == []
