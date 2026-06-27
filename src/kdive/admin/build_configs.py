"""Build-config seeding command helper."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import psycopg

import kdive.config as config
from kdive.config.core_settings import DATABASE_URL


def seed_build_configs_step(database_url: str | None = None) -> int:
    """Publish the packaged build-config fragments (the deploy ``seed-build-configs`` step).

    Re-homed out of ``migrate()`` (ADR-0121). S3-gated + idempotent: a wholly-unconfigured object
    store is a clean skip (returns 0); a configured-but-broken store (missing bucket, bad
    credentials) raises — a real object-store fault must surface, not be swallowed.

    Args:
        database_url: A psycopg connection string, or ``None`` to read ``KDIVE_DATABASE_URL``.

    Returns:
        The number of build-config fragments published (0 if already current or skipped).
    """
    url = database_url or config.require(DATABASE_URL)
    seeded = _seed_build_configs_step(url)
    print(f"seeded {seeded} build-config fragment(s)")
    return seeded


def _run_async_db_step(
    database_url: str, step: Callable[[psycopg.AsyncConnection], Awaitable[int]]
) -> int:
    import asyncio

    async def _run() -> int:
        async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
            return await step(conn)

    return asyncio.run(_run())


def _seed_build_configs_step(database_url: str) -> int:
    """Publish the packaged build-config fragments after migrating (ADR-0096).

    Runs in the deploy ``migrate -> seed`` step. Idempotent (sha256-gated). The fragments
    live in the object store, so the seed is skipped when ``KDIVE_S3_*`` is unconfigured —
    a no-S3 migrate (e.g. a schema-only test or a partial bring-up) degrades cleanly and the
    fragment is seeded on a later migrate once the object store is available. Mirrors the
    optional object-store policy in :mod:`kdive.store.assembly`.

    Args:
        database_url: A psycopg-compatible connection string for the application database.

    Returns:
        The number of build-config fragments published (0 if already current or skipped).
    """
    from kdive.build_configs.seed import seed_build_configs
    from kdive.domain.errors import CategorizedError, ErrorCategory
    from kdive.store.objectstore import object_store_from_env

    try:
        store = object_store_from_env()
    except CategorizedError as exc:
        if exc.category is not ErrorCategory.CONFIGURATION_ERROR:
            raise
        print("skipped build-config seed: object store not configured")
        return 0

    async def _seed(conn: psycopg.AsyncConnection) -> int:
        return await seed_build_configs(conn, store)

    return _run_async_db_step(database_url, _seed)
