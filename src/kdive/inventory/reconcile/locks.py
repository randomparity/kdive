"""Inventory reconcile advisory locks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection

from kdive.db.locks import (
    INVENTORY_RECONCILE,
    LockScope,
    advisory_xact_lock,
    session_advisory_lock,
)
from kdive.domain.catalog.resources import ResourceKind


def resource_identity_lock_key(kind: ResourceKind, name: str) -> str:
    """The advisory-lock key serializing mutation of one ``(kind, name)`` resource identity."""
    return f"{kind.value}:{name}"


@asynccontextmanager
async def resource_identity_lock(
    conn: AsyncConnection, kind: ResourceKind, name: str
) -> AsyncIterator[None]:
    """Hold the transaction-scoped per-identity lock for ``(kind, name)`` over the block."""
    async with advisory_xact_lock(conn, LockScope.RESOURCE, resource_identity_lock_key(kind, name)):
        yield


@asynccontextmanager
async def inventory_pass_lock(conn: AsyncConnection) -> AsyncIterator[None]:
    """Hold the session-scoped inventory lock for a whole reconcile pass."""
    async with session_advisory_lock(conn, INVENTORY_RECONCILE):
        yield


__all__ = ["inventory_pass_lock", "resource_identity_lock", "resource_identity_lock_key"]
