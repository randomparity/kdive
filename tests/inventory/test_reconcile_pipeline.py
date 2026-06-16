"""The shared ordered reconcile pipeline runs coefficients before resources (ADR-0115 §2)."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from psycopg import AsyncConnection

from kdive.inventory import reconcile_pipeline
from kdive.inventory.model import InventoryDoc
from kdive.inventory.reconcile import ReconcileDiff
from kdive.inventory.reconcile_images import ImageHeadStore


def _recorder(name: str, calls: list[str]):
    async def _fn(*_args: object, **_kwargs: object) -> ReconcileDiff:
        calls.append(name)
        return ReconcileDiff()

    return _fn


def test_pipeline_invokes_coefficients_before_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    for name in (
        "reconcile_images",
        "reconcile_coefficients",
        "reconcile_resources",
        "reconcile_build_hosts",
        "reconcile_build_configs",
    ):
        monkeypatch.setattr(reconcile_pipeline, name, _recorder(name, calls))

    # The sub-passes are monkeypatched, so the args are inert sentinels; cast satisfies the
    # signature without standing up a real connection/doc/store.
    asyncio.run(
        reconcile_pipeline.reconcile_all(
            cast("AsyncConnection", object()),
            cast("InventoryDoc", object()),
            cast("ImageHeadStore", object()),
        )
    )

    # The load-bearing invariant: a host's price is upserted before its row is reconciled.
    assert calls.index("reconcile_coefficients") < calls.index("reconcile_resources")
    assert calls == [
        "reconcile_images",
        "reconcile_coefficients",
        "reconcile_resources",
        "reconcile_build_hosts",
        "reconcile_build_configs",
    ]
