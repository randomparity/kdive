"""Shared reconciler test helpers.

``ReconcileConfig`` requires an ``upload_store`` and ``image_store`` (S3 is a required
backend, ADR-0337), and ``reconcile_once`` no longer skips the store passes when a store
is absent. Tests that exercise an *unrelated* pass use :func:`make_reconcile_config`,
which supplies inert stores whose sweep queries find nothing to do, so the store passes
run as harmless no-ops without polluting ``report.failures``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

from kdive.reconciler.loop import ReconcileConfig, ReconcileUploadStore
from kdive.services.images.retention import ImageSweepStore


class _NullImageStore:
    def list_image_objects(self) -> list[Any]:
        return []

    def head_present(self, key: str) -> bool:
        return True

    def delete(self, key: str) -> None:
        return None

    def put_artifact(self, request: Any) -> Any:
        raise NotImplementedError("null image store does not upload artifacts")


class _NullUploadStore:
    def list_prefix(self, prefix: str) -> list[str]:
        return []

    def iter_prefix_pages_with_mtime(self, prefix: str) -> Iterator[list[Any]]:
        yield []  # one empty page, as list_objects_v2 replies for a prefix matching nothing

    def head(self, key: str) -> Any:
        return None

    def delete(self, key: str) -> None:
        return None

    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        assert limit == 20
        return True


def null_image_store() -> ImageSweepStore:
    """An inert ``ImageSweepStore`` for inventory-pass tests with no s3 images."""
    return cast(ImageSweepStore, _NullImageStore())


def make_reconcile_config(**overrides: Any) -> ReconcileConfig:
    """Build a ``ReconcileConfig`` with inert default stores for store-agnostic tests."""
    upload_store: ReconcileUploadStore = _NullUploadStore()
    defaults: dict[str, Any] = {
        "upload_store": upload_store,
        "image_store": cast(ImageSweepStore, _NullImageStore()),
    }
    defaults.update(overrides)
    return ReconcileConfig(**defaults)
