"""Fail-open reader for a Run's uploaded ``effective_config`` artifact (ADR-0318).

The config is SENSITIVE and Run-owned. This returns a parsed :class:`KernelConfig` only when a
real config is present; every failure mode (no row, store/DB error, degenerate parse) returns
``None`` so the caller arms as today rather than converting a benign advisory read into an
install/vmcore failure.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg import AsyncConnection

from kdive.artifacts.storage import FetchedArtifact
from kdive.domain.errors import CategorizedError
from kdive.kernel_config.parse import KernelConfig, parse_kernel_config
from kdive.services.artifacts.read_model import effective_config_key
from kdive.store.objectstore import object_store_from_env

_log = logging.getLogger(__name__)


class ConfigStore(Protocol):
    """The narrow object-store capability the reader needs (an ObjectStore satisfies it)."""

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact: ...


async def load_effective_config(
    conn: AsyncConnection,
    run_id: UUID,
    *,
    store_factory: Callable[[], ConfigStore] = object_store_from_env,
) -> KernelConfig | None:
    """Return the Run's uploaded kernel config, or ``None`` when it cannot be read/trusted.

    ``None`` (arm-as-today) covers: no uploaded config, any store/DB error, and a degenerate
    (zero-enabled-symbol) upload. Never raises — the gate must not turn a config read into an
    action failure.
    """
    try:
        key = await effective_config_key(conn, run_id)
        if key is None:
            return None
        # Build the store and fetch on a worker thread — object_store_from_env constructs a
        # boto3 client (blocking) and get_artifact does blocking I/O; keep both off the loop.
        fetched = await asyncio.to_thread(lambda: store_factory().get_artifact(key, None))
    except (CategorizedError, psycopg.Error, OSError) as exc:
        _log.warning("effective_config read failed for run %s; arming as today: %s", run_id, exc)
        return None
    config = parse_kernel_config(fetched.data)
    if config.is_degenerate:
        _log.warning("effective_config for run %s is degenerate; arming as today", run_id)
        return None
    return config
