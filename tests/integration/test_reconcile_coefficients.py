"""DB-backed tests for the cost-class coefficient reconcile pass (ADR-0115 §2/§3)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.inventory.model import InventoryDoc
from kdive.inventory.reconcile_coefficients import reconcile_coefficients


def _doc(*classes: tuple[str, str]) -> InventoryDoc:
    return InventoryDoc.parse(
        {
            "schema_version": 2,
            "cost_class": [{"name": n, "coeff": c} for n, c in classes],
        }
    )


async def _coeff(pool: AsyncConnectionPool, name: str) -> Decimal | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT coeff FROM cost_class_coefficients WHERE cost_class = %s", (name,)
        )
        row = await cur.fetchone()
    return Decimal(row[0]) if row else None


def test_upserts_a_new_declared_coefficient(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                diff = await reconcile_coefficients(conn, _doc(("premium", "2.5")))
            assert await _coeff(pool, "premium") == Decimal("2.5")
            assert [r.name for r in diff.created] == ["premium"]
            assert diff.warned == []

    asyncio.run(_run())


def test_file_value_overrides_existing_row_and_flags_drift(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            # Seed a runtime override that differs from the file.
            async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
                await seed.execute(
                    "INSERT INTO cost_class_coefficients (cost_class, coeff) "
                    "VALUES ('remote', 9.0) "
                    "ON CONFLICT (cost_class) DO UPDATE SET coeff = EXCLUDED.coeff"
                )
            async with pool.connection() as conn:
                diff = await reconcile_coefficients(conn, _doc(("remote", "1.0")))
            assert await _coeff(pool, "remote") == Decimal("1.0")
            assert [r.name for r in diff.updated] == ["remote"]
            drift = [r for r in diff.warned if r.name == "remote"]
            assert drift and "was 9.0" in drift[0].detail and "now 1.0" in drift[0].detail

    asyncio.run(_run())


def test_idempotent_rerun_is_a_clean_no_op(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_coefficients(conn, _doc(("premium", "2.5")))
            async with pool.connection() as conn:
                diff = await reconcile_coefficients(conn, _doc(("premium", "2.5")))
            assert diff.created == [] and diff.updated == [] and diff.warned == []

    asyncio.run(_run())


def test_removed_block_does_not_delete(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_coefficients(conn, _doc(("premium", "2.5")))
            # A later pass with no [[cost_class]] block leaves the row untouched.
            async with pool.connection() as conn:
                diff = await reconcile_coefficients(conn, _doc())
            assert await _coeff(pool, "premium") == Decimal("2.5")
            assert diff.pruned == []

    asyncio.run(_run())


def test_undeclared_seed_floor_untouched(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_coefficients(conn, _doc(("premium", "2.5")))
            # 'local' is seeded by 0002 and never declared here; it must keep its value.
            assert await _coeff(pool, "local") == Decimal("1.0")

    asyncio.run(_run())


def test_file_overrides_a_migration_seed(migrated_url: str) -> None:
    # ADR-0115 §4: declaring a seeded class in the file overrides the seed default and flags
    # drift — the path by which an operator reprices the built-in 'local'/'remote' baselines.
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            # 'local' starts at the 0002 seed (1.0), with no prior runtime write.
            assert await _coeff(pool, "local") == Decimal("1.0")
            async with pool.connection() as conn:
                diff = await reconcile_coefficients(conn, _doc(("local", "2.0")))
            assert await _coeff(pool, "local") == Decimal("2.0")
            assert [r.name for r in diff.updated] == ["local"]
            drift = [r for r in diff.warned if r.name == "local"]
            assert drift and "was 1.0" in drift[0].detail and "now 2.0" in drift[0].detail

    asyncio.run(_run())
