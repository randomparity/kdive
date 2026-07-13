"""Object-store composition policies for process assembly."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from kdive.store.objectstore import ObjectStore, object_store_from_env

ObjectStoreFactory = Callable[[], ObjectStore]


@dataclass(frozen=True, slots=True)
class ObjectStoreAssembly:
    """The process object store, assembled once for app and worker wiring."""

    store: ObjectStore
    request_time_store_factory: ObjectStoreFactory


def build_object_store_assembly(
    store_factory: ObjectStoreFactory | None = None,
) -> ObjectStoreAssembly:
    """Resolve the process object store.

    S3 is a required backend (ADR-0337): ``object_store_from_env`` raises a
    ``configuration_error`` when it is unconfigured, and ``config.validate`` already
    rejects that at startup, so ``store`` is always a live :class:`ObjectStore`.
    ``request_time_store_factory`` is retained for request-time lazy construction.
    """
    store_factory = store_factory or object_store_from_env
    return ObjectStoreAssembly(
        store=store_factory(),
        request_time_store_factory=store_factory,
    )
