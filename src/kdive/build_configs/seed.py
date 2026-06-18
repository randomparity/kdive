"""App-level build-config seed (ADR-0096).

The SQL migration creates the table; this step publishes the packaged kdump fragment to a
fixed reserved object-store key and upserts the catalog row, idempotently. The bytes go to the
object store via ``put_artifact`` (object-store write only — NOT ``register_artifact_row``, so
no project-scoped artifacts row and none of its TTL/owner lifecycle, per ADR-0096). The reserved
key is deterministic in (tenant, owner_kind, owner_id, name), so an edited fragment overwrites
in place — no orphaned object. ``Sensitivity.REDACTED`` marks the fragment serve-eligible (the
``buildconfig.get`` tool serves it); it carries no secret.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from psycopg import AsyncConnection

from kdive.artifacts.storage import ArtifactWriteRequest
from kdive.build_configs.catalog import read_build_config_provenance, upsert_seed_build_config
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.store.objectstore import ObjectStore

KDUMP_FRAGMENT_PATH = Path(__file__).parent / "data" / "kdump.config"
_KDUMP_NAME = "kdump"
_KDUMP_DESCRIPTION = "kdump/debuginfo kernel-config fragment"
_TENANT = "system"
_OWNER_KIND = "build-configs"
_RETENTION_CLASS = "build-config"


async def seed_build_configs(conn: AsyncConnection, store: ObjectStore) -> int:
    """Publish the packaged kdump fragment + upsert its row, source-aware. Returns 0 or 1.

    Serialized per fragment name on :attr:`LockScope.BUILD_CONFIG` — the same lock
    ``buildconfig.set`` takes — inside an explicit transaction (the migrate connection is
    autocommit, and the advisory lock needs an open transaction), so a concurrent operator
    ``set`` cannot interleave with the read/PUT/upsert and the seed never PUTs over an operator
    override (ADR-0119). Idempotent: an unchanged seed-owned fragment writes nothing; an
    operator-owned **or config-owned** row is skipped (a declared ``[[build_config]]`` is
    file-authoritative, ADR-0122). The ``WHERE source='seed'`` guard on
    :func:`upsert_seed_build_config` is defence in depth on the row.

    Args:
        conn: An open async psycopg connection (autocommit recommended).
        store: The object store to publish bytes into.

    Returns:
        The number of fragments published (0 if skipped, 1 if published/updated).
    """
    data = KDUMP_FRAGMENT_PATH.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.BUILD_CONFIG, _KDUMP_NAME):
        stored = await read_build_config_provenance(conn, _KDUMP_NAME)
        if stored is not None and (
            (stored[0] == sha256 and stored[1] == "seed") or stored[1] in {"operator", "config"}
        ):
            return 0
        written = store.put_artifact(
            ArtifactWriteRequest(
                tenant=_TENANT,
                owner_kind=_OWNER_KIND,
                owner_id=_KDUMP_NAME,
                name="kdump.config",
                data=data,
                sensitivity=Sensitivity.REDACTED,
                retention_class=_RETENTION_CLASS,
            )
        )
        await upsert_seed_build_config(conn, _KDUMP_NAME, written.key, sha256, _KDUMP_DESCRIPTION)
    return 1
