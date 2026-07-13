"""Shared reconciler test helpers.

``ReconcileConfig`` requires an ``upload_store`` and ``image_store`` (S3 is a required
backend, ADR-0337), and ``reconcile_once`` no longer skips the store passes when a store
is absent. Tests that exercise an *unrelated* pass use :func:`make_reconcile_config`,
which supplies inert stores whose sweep queries find nothing to do, so the store passes
run as harmless no-ops without polluting ``report.failures``.
"""

from __future__ import annotations

from typing import Any, cast

from kdive.reconciler.cleanup.uploads import UploadStore
from kdive.reconciler.loop import ReconcileConfig
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

    def delete(self, key: str) -> None:
        return None


def null_image_store() -> ImageSweepStore:
    """An inert ``ImageSweepStore`` for inventory-pass tests with no s3 images."""
    return cast(ImageSweepStore, _NullImageStore())


def make_reconcile_config(**overrides: Any) -> ReconcileConfig:
    """Build a ``ReconcileConfig`` with inert default stores for store-agnostic tests."""
    defaults: dict[str, Any] = {
        "upload_store": cast(UploadStore, _NullUploadStore()),
        "image_store": cast(ImageSweepStore, _NullImageStore()),
    }
    defaults.update(overrides)
    return ReconcileConfig(**defaults)
