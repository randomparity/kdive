"""Synchronous rootfs catalog fetch for the local-libvirt provision lane (ADR-0228).

Wires the previously-unwired local-libvirt ``catalog`` rootfs lane: a ``catalog`` rootfs reference
now resolves at provision time. Mirrors ``build_config_fetch_from_env`` — a synchronous callable
that lazily opens its own resources per call, because the provider provision seam runs off the
event loop (``asyncio.to_thread``) and owns no async pool.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

import kdive.config as config
from kdive.components.references import CatalogComponentRef
from kdive.config.core_settings import DATABASE_URL
from kdive.images.fetch import fetch_registered_rootfs_sync
from kdive.providers.local_libvirt.lifecycle.rootfs.materialize import CatalogFetch
from kdive.providers.local_libvirt.lifecycle.storage import ROOTFS_DIR
from kdive.store.objectstore import object_store_from_env

# The s3-fetch cache lives OUTSIDE allowed_roots (which default to [ROOTFS_DIR]) so a cached image
# is never reachable as a staged-path candidate, keeping the no-escape invariant true.
_CACHE_DIR = Path(ROOTFS_DIR).parent / "rootfs-cache"


def rootfs_catalog_fetch_from_env(allowed_roots: list[Path]) -> CatalogFetch:
    """A synchronous ``(ref, arch) -> Path`` rootfs catalog fetch (ADR-0228).

    Opens a short-lived sync ``psycopg`` connection per call (the provision seam runs in a thread
    and owns no async pool). Resolves the registered **public** image of ``arch`` and branches: a
    staged-path row validates its host path against ``allowed_roots`` (**no object store touched**);
    an s3 row builds the object store lazily, downloads + digest-verifies + caches under
    ``_CACHE_DIR``. Passing ``object_store_from_env`` as a factory keeps staged-path provisioning
    working when no object storage is configured (the no-S3 lane).
    """

    def _fetch(ref: CatalogComponentRef, arch: str) -> Path:
        with psycopg.connect(config.require(DATABASE_URL)) as conn:
            return fetch_registered_rootfs_sync(
                conn,
                object_store_from_env,
                allowed_roots=allowed_roots,
                provider=ref.provider,
                name=ref.name,
                arch=arch,
                cache_dir=_CACHE_DIR,
            )

    return _fetch
