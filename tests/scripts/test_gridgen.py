from __future__ import annotations

from typing import Any

import pytest

import scripts.coverage_campaign.gridgen as gridgen
from kdive.store.assembly import ObjectStoreAssembly
from scripts.coverage_campaign.gridgen import generate_rows


def test_generate_rows_covers_known_tools_with_correct_metadata() -> None:
    rows = {r.tool: r for r in generate_rows()}
    assert rows["resources.list"].annotation == "read_only"
    assert rows["resources.list"].plane == "resources"
    assert rows["runs.complete_build"].maturity == "implemented"
    assert rows["runs.complete_build"].annotation == "mutating"
    assert rows["control.force_crash"].annotation == "destructive"
    assert rows["control.force_crash"].destructive_member is True
    assert all(r.plane for r in rows.values())
    assert all(r.maturity in {"implemented", "partial", "planned"} for r in rows.values())


def test_generate_rows_is_nonempty_and_unique() -> None:
    rows = generate_rows()
    names = [r.tool for r in rows]
    assert len(names) > 50
    assert len(names) == len(set(names))


def test_grid_generation_injects_an_offline_object_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _App:
        async def list_tools(self) -> list[Any]:
            return []

    def _build_app(*args: Any, **kwargs: Any) -> _App:
        captured.update(kwargs)
        return _App()

    monkeypatch.setattr(gridgen, "build_app_from_assembly", _build_app)

    assert gridgen._build_tools() == []
    process = captured["process_assembly"]
    assert isinstance(process.object_stores, ObjectStoreAssembly)
    assert process.object_stores.store is not None
