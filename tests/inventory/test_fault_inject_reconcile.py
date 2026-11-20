"""Reconcile reports a malformed fault-inject capability key instead of leaving it for binding."""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.inventory.model import InventoryDoc
from kdive.inventory.reconcile.resources import reconcile_resources
from kdive.providers.fault_inject.capabilities import FAULT_RATE_KEY, SEED_KEY


def _doc(**changes: Any) -> InventoryDoc:
    return InventoryDoc.parse(
        {
            "schema_version": 2,
            "fault_inject": [
                {"name": "fi-1", "cost_class": "local", "vcpus": 2, "memory_mb": 2048, **changes}
            ],
        }
    )


async def _capabilities(conn: psycopg.AsyncConnection) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT capabilities FROM resources WHERE kind='fault-inject'")
        row = await cur.fetchone()
    assert row is not None
    return row["capabilities"]


async def _hand_edit(conn: psycopg.AsyncConnection, edit: dict[str, Any]) -> None:
    await conn.execute(
        "UPDATE resources SET capabilities = capabilities || %s WHERE kind='fault-inject'",
        (Jsonb(edit),),
    )


@pytest.mark.parametrize(
    ("edit", "key"),
    [
        ({FAULT_RATE_KEY: 0}, FAULT_RATE_KEY),
        ({FAULT_RATE_KEY: {"provision": 2.0}}, FAULT_RATE_KEY),
        ({SEED_KEY: "7"}, SEED_KEY),
    ],
)
def test_reconcile_warns_on_a_malformed_row_and_leaves_it_unchanged(
    migrated_url: str, edit: dict[str, Any], key: str
) -> None:
    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await reconcile_resources(conn, _doc())
            await _hand_edit(conn, edit)
            before = await _capabilities(conn)

            diff = await reconcile_resources(conn, _doc(vcpus=4))

            assert [r.name for r in diff.warned] == ["fi-1"]
            assert f"{key}={edit[key]!r}" in diff.warned[0].detail
            assert not diff.updated
            assert await _capabilities(conn) == before

    asyncio.run(run())


def test_reconcile_applies_a_change_to_a_well_formed_edited_row(migrated_url: str) -> None:
    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await reconcile_resources(conn, _doc())
            await _hand_edit(conn, {FAULT_RATE_KEY: {"provision": 0.5}})

            diff = await reconcile_resources(conn, _doc(vcpus=4))

            assert not diff.warned
            caps = await _capabilities(conn)
            assert caps["vcpus"] == 4
            assert caps[FAULT_RATE_KEY] == {"provision": 0.5}

    asyncio.run(run())
