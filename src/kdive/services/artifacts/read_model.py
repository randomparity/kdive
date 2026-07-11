"""Artifact read-model helpers shared across MCP, jobs, and providers."""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection, Connection

from kdive.db import artifact_queries
from kdive.db.artifact_queries import RunFetchContext

RUN_ARTIFACT_NAMES = frozenset({"effective_config", "kernel", "initrd", "vmlinux"})
SYSTEM_ARTIFACT_NAMES = frozenset({"rootfs"})


async def run_fetch_context(conn: AsyncConnection, run_id: UUID) -> RunFetchContext | None:
    """Return the Run fetch context for raw artifact egress."""
    return await artifact_queries.run_fetch_context(conn, run_id)


async def system_project(conn: AsyncConnection, system_id: UUID) -> str | None:
    """Return a System's owning project, or ``None`` when the row is absent."""
    return await artifact_queries.system_project(conn, system_id)


async def raw_vmcore_key(conn: AsyncConnection, run_id: UUID) -> str | None:
    """Return the Run-owned raw vmcore object key, or ``None``."""
    return await artifact_queries.raw_vmcore_key(conn, run_id)


async def effective_config_key(conn: AsyncConnection, run_id: UUID) -> str | None:
    """Return the Run-owned effective config object key, or ``None``."""
    return await artifact_queries.effective_config_key(conn, run_id)


def debuginfo_ref_for_run_sync(conn: Connection, run_id: UUID) -> str | None:
    """Return the Run's published debuginfo object key, or ``None``."""
    return artifact_queries.debuginfo_ref_for_run_sync(conn, run_id)


def kernel_ref_for_run_sync(conn: Connection, run_id: UUID) -> str | None:
    """Return the Run's published combined kernel/modules object key, or ``None``."""
    return artifact_queries.kernel_ref_for_run_sync(conn, run_id)
