"""Inert object-store wiring for tests that exercise no object behavior."""

from __future__ import annotations

from typing import cast

from kdive.store.objectstore import ObjectStore


class _InertObjectStore:
    def head(self, _key: str) -> None:
        return None

    def delete_version(self, _key: str, _version_id: str) -> None:
        return None

    def delete_retired_key_batch(self, _key: str, _limit: int) -> bool:
        return True


INERT_OBJECT_STORE = cast(ObjectStore, _InertObjectStore())
