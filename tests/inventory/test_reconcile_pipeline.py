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


def _recorder(name: str, calls: list[str], received: dict[str, tuple[object, ...]]):
    async def _fn(*args: object, **_kwargs: object) -> ReconcileDiff:
        calls.append(name)
        received[name] = args
        return ReconcileDiff()

    return _fn


def _int_recorder(name: str, calls: list[str], received: dict[str, tuple[object, ...]]):
    """A recorder for the override GC step, which returns an ``int`` (the cleared count)."""

    async def _fn(*args: object, **_kwargs: object) -> int:
        calls.append(name)
        received[name] = args
        return 0

    return _fn


def test_pipeline_invokes_coefficients_before_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    received: dict[str, tuple[object, ...]] = {}
    for name in (
        "reconcile_images",
        "reconcile_coefficients",
        "reconcile_resources",
        "reconcile_build_hosts",
        "reconcile_build_configs",
    ):
        monkeypatch.setattr(reconcile_pipeline, name, _recorder(name, calls, received))
    monkeypatch.setattr(
        reconcile_pipeline,
        "reconcile_overrides_gc",
        _int_recorder("reconcile_overrides_gc", calls, received),
    )

    # The sub-passes are monkeypatched, so the args are inert sentinels; cast satisfies the
    # signature without standing up a real connection/doc/store. Distinct objects let each
    # recorder assert it received the right one in the right position.
    conn = object()
    doc = object()
    store = object()
    asyncio.run(
        reconcile_pipeline.reconcile_all(
            cast("AsyncConnection", conn),
            cast("InventoryDoc", doc),
            cast("ImageHeadStore", store),
        )
    )

    # The load-bearing invariant: a host's price is upserted before its row is reconciled.
    assert calls.index("reconcile_coefficients") < calls.index("reconcile_resources")
    # The override GC runs last, after the resource/build-host passes apply the doc (ADR-0199).
    assert calls == [
        "reconcile_images",
        "reconcile_coefficients",
        "reconcile_resources",
        "reconcile_build_hosts",
        "reconcile_build_configs",
        "reconcile_overrides_gc",
    ]

    # Each pass receives exactly the orchestrator's connection/doc (and store where applicable)
    # in the documented positional order — a misforwarded or dropped argument is a real bug.
    assert received["reconcile_images"] == (conn, doc, store)
    assert received["reconcile_coefficients"] == (conn, doc)
    assert received["reconcile_resources"] == (conn, doc)
    assert received["reconcile_build_hosts"] == (conn, doc)
    assert received["reconcile_build_configs"] == (conn, doc, store)
    assert received["reconcile_overrides_gc"] == (conn, doc)
