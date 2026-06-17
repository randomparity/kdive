"""The ``[[build_config]]`` merge-reconcile (#443, ADR-0122).

Publishes each declared ``[[build_config]]`` fragment's inline ``content`` to the reserved
build-config object key and upserts its ``build_config_catalog`` row ``source='config'``,
file-authoritatively. Each fragment is handled under the shared per-name ``BUILD_CONFIG``
advisory lock (the same lock the seed and ``buildconfig.set`` take), so reconcile, seed, and
set never interleave a row sha256 that describes another writer's bytes (the ADR-0119
row-vs-object contract). Upsert-only -- never prunes (ADR-0122 §2).

The pass needs a publish-capable store. The reconciler loop and the on-demand path (with S3
configured) pass a concrete ``ObjectStore``; the on-demand ``_AbsentImageStore`` (no S3) cannot
publish, so the pass warns and skips -- the same degrade the image pass uses. The byte cap is
enforced here off the same ``MAX_BUILD_CONFIG_BYTES`` the tool and ``--check`` read.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Protocol, runtime_checkable

from psycopg import AsyncConnection

import kdive.config as config
from kdive.artifacts.storage import ArtifactWriteRequest
from kdive.build_configs.catalog import read_build_config_provenance, upsert_config_build_config
from kdive.build_configs.rules import exceeds_build_config_cap
from kdive.config.core_settings import MAX_BUILD_CONFIG_BYTES
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.models import Sensitivity
from kdive.inventory.errors import InventoryError
from kdive.inventory.model import BuildConfigDecl, InventoryDoc
from kdive.inventory.reconcile import ReconcileDiff, ReconcileRecord

_log = logging.getLogger(__name__)

_TENANT = "system"
_OWNER_KIND = "build-configs"
_RETENTION_CLASS = "build-config"


@runtime_checkable
class BuildConfigPublishStore(Protocol):
    """The publish-capable store port the build-config reconcile needs (write + presence)."""

    def head_present(self, key: str) -> bool: ...
    def put_artifact(self, request: ArtifactWriteRequest) -> object: ...


def _record(name: str, detail: str = "") -> ReconcileRecord:
    return ReconcileRecord(name=name, entry=f"build_config[{name}]", detail=detail)


async def reconcile_build_configs(
    conn: AsyncConnection, doc: InventoryDoc, store: object
) -> ReconcileDiff:
    """Publish each ``[[build_config]]`` declaration file-authoritatively; return the diff.

    Args:
        conn: A transaction-free pooled connection (each fragment opens its own transaction
            to hold the per-name advisory lock).
        doc: The parsed inventory document.
        store: The reconcile store. Must satisfy :class:`BuildConfigPublishStore` (head + put)
            to publish; a head-only store degrades every declared fragment to ``warned``.

    Returns:
        The :class:`ReconcileDiff` for the build-config pass (``created``/``updated`` per
        published row, ``warned`` for an over-cap skip, a store that cannot publish, or a real
        clobber).
    """
    diff = ReconcileDiff()
    if not doc.build_config:
        return diff
    if not isinstance(store, BuildConfigPublishStore):
        for frag in doc.build_config:
            diff.warned.append(_record(frag.name, "object store cannot publish; row untouched"))
        _log.warning("inventory: build_config pass has no publish-capable store; skipped")
        return diff
    cap = int(config.require(MAX_BUILD_CONFIG_BYTES))
    for frag in doc.build_config:
        await _reconcile_one(conn, frag, store, cap, diff)
    return diff


async def _reconcile_one(
    conn: AsyncConnection,
    frag: BuildConfigDecl,
    store: BuildConfigPublishStore,
    cap: int,
    diff: ReconcileDiff,
) -> None:
    """Publish one fragment under its per-name lock, change-detecting and warning on a clobber."""
    data = frag.content.encode("utf-8")
    if exceeds_build_config_cap(data, cap):
        diff.warned.append(_record(frag.name, f"content exceeds {cap} bytes; skipped"))
        _log.warning("inventory: build_config %r over cap (%d bytes); skipped", frag.name, cap)
        return
    sha256 = hashlib.sha256(data).hexdigest()
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.BUILD_CONFIG, frag.name):
        prior = await read_build_config_provenance(conn, frag.name)
        if prior is not None and prior == (sha256, "config", frag.description):
            return  # idempotent: no write, no diff, no log noise
        written = store.put_artifact(
            ArtifactWriteRequest(
                tenant=_TENANT,
                owner_kind=_OWNER_KIND,
                owner_id=frag.name,
                name=f"{frag.name}.config",
                data=data,
                sensitivity=Sensitivity.REDACTED,
                retention_class=_RETENTION_CLASS,
            )
        )
        await upsert_config_build_config(
            conn, frag.name, _key_of(written), sha256, frag.description
        )
        if prior is None:
            diff.created.append(_record(frag.name))
            return
        diff.updated.append(_record(frag.name))
        # Warn only on a real clobber: an operator override reverted, or the bytes changed. A
        # benign seed->config adoption at identical bytes, or a description-only edit on an
        # already-config row, is `updated`, not `warned` (mirrors reconcile_coefficients).
        if prior[1] == "operator" or prior[0] != sha256:
            detail = f"re-asserted from file over {prior[1]} (was sha {prior[0][:12]})"
            diff.warned.append(_record(frag.name, detail))
            _log.warning("inventory: build_config %r %s", frag.name, detail)


def _key_of(written: object) -> str:
    """Read the object key off the ``put_artifact`` result (``.key``)."""
    key = getattr(written, "key", None)
    if not isinstance(key, str):
        raise InventoryError(
            "build_config",
            "object_key",
            "object store returned an artifact without a string key",
        )
    return key
