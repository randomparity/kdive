"""Object-store composition policies for process assembly."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import kdive.config as config
from kdive.config.core_settings import S3_BUCKET, S3_ENDPOINT_URL, S3_REGION
from kdive.domain.errors import CategorizedError
from kdive.store.objectstore import ObjectStore, object_store_from_env

ObjectStoreFactory = Callable[[], ObjectStore]
RequiredObjectStore = ObjectStore | CategorizedError

_S3_OPTIONAL_ENV_NAMES = frozenset({S3_ENDPOINT_URL.name, S3_BUCKET.name, S3_REGION.name})


@dataclass(frozen=True, slots=True)
class ObjectStoreAssembly:
    """Named object-store policies assembled once for app and worker wiring."""

    optional_upload_store: ObjectStore | None
    optional_image_store: ObjectStore | None
    optional_ops_image_store: ObjectStore | None
    required_image_build_store: RequiredObjectStore
    request_time_store_factory: ObjectStoreFactory


def build_object_store_assembly(
    store_factory: ObjectStoreFactory | None = None,
) -> ObjectStoreAssembly:
    """Resolve process-level object-store roles with one absence/error policy.

    Optional stores are disabled only when all ``KDIVE_S3_*`` settings are absent. Partial or
    invalid S3 config raises during assembly. The image-build handler is always registered, so its
    required store captures the absent-store configuration error for later job failure.
    """
    store_factory = store_factory or object_store_from_env
    optional_store = optional_object_store(store_factory)
    return ObjectStoreAssembly(
        optional_upload_store=optional_store,
        optional_image_store=optional_store,
        optional_ops_image_store=optional_store,
        required_image_build_store=optional_store or _required_store_error(store_factory),
        request_time_store_factory=store_factory,
    )


def optional_object_store(
    store_factory: ObjectStoreFactory | None = None,
) -> ObjectStore | None:
    """Return an object store, or ``None`` only when S3 is wholly unconfigured."""
    store_factory = store_factory or object_store_from_env
    try:
        return store_factory()
    except CategorizedError:
        if s3_env_is_absent():
            return None
        raise


def s3_env_is_absent() -> bool:
    """Whether none of the S3 settings is present in the current environment."""
    return _S3_OPTIONAL_ENV_NAMES.isdisjoint(config.env_snapshot())


def _required_store_error(store_factory: ObjectStoreFactory) -> RequiredObjectStore:
    try:
        return store_factory()
    except CategorizedError as error:
        return error
