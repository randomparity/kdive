"""CWD-shadow tests for the reconciler inventory pass (kept separate from the mutation sweep).

``_cwd_inventory_shadowed`` is inherently CWD-relative (it probes ``Path("systems.toml")``), so
its tests use ``monkeypatch.chdir``. mutmut's trampoline resolves the mutated source file's path
relative to the process CWD, so a ``chdir`` mid-test aborts its baseline — these tests therefore
live in their own module and are NOT passed to ``just mutate`` for ``reconciler/inventory.py``.
They still run in the normal suite for behavioral coverage; the sibling
``test_inventory_pass.py`` (no ``chdir``) drives the mutation sweep.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from psycopg import AsyncConnection

from kdive.inventory.reconcile.images import ImageHeadStore
from kdive.inventory.reconcile.records import ReconcileDiff
from kdive.reconciler import inventory as inv
from kdive.reconciler.inventory import InventoryReconcilePass, _cwd_inventory_shadowed

_CONN = cast("AsyncConnection[Any]", object())


class _FakeStore:
    def head_present(self, key: str) -> bool:  # pragma: no cover - never called
        return False

    def list_image_objects(self) -> list[Any]:  # pragma: no cover
        return []


def _store() -> ImageHeadStore:
    return cast(ImageHeadStore, _FakeStore())


def test_cwd_shadowed_true_when_unset_default_absent_and_cwd_file_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("KDIVE_SYSTEMS_TOML", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "systems.toml").write_text("schema_version = 2\n")
    assert _cwd_inventory_shadowed(tmp_path / "xdg" / "systems.toml") is True


def test_cwd_shadowed_false_when_var_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "systems.toml").write_text("schema_version = 2\n")
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "elsewhere.toml"))
    assert _cwd_inventory_shadowed(tmp_path / "xdg" / "systems.toml") is False


def test_cwd_shadowed_false_when_default_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("KDIVE_SYSTEMS_TOML", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "systems.toml").write_text("schema_version = 2\n")
    present_default = tmp_path / "default.toml"
    present_default.write_text("schema_version = 2\n")
    assert _cwd_inventory_shadowed(present_default) is False


def test_cwd_shadowed_false_when_no_cwd_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("KDIVE_SYSTEMS_TOML", raising=False)
    monkeypatch.chdir(tmp_path)
    assert _cwd_inventory_shadowed(tmp_path / "xdg" / "systems.toml") is False


def test_cwd_shadowed_true_when_var_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An explicitly-empty var resolves to the XDG default just like unset, so still shadowed.
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", "")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "systems.toml").write_text("schema_version = 2\n")
    assert _cwd_inventory_shadowed(tmp_path / "xdg" / "systems.toml") is True


def test_run_warns_once_about_shadowed_cwd_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The shadow warning fires at most once per instance even though _load() calls reset() every
    # pass while the resolved default is absent (the shadow condition itself).
    monkeypatch.delenv("KDIVE_SYSTEMS_TOML", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "systems.toml").write_text("schema_version = 2\n")  # the shadowed CWD file
    absent_default = tmp_path / "xdg" / "systems.toml"
    monkeypatch.setattr(inv, "systems_toml_path", lambda: absent_default)
    monkeypatch.setattr(
        inv,
        "load_inventory_optional",
        lambda p: pytest.fail("the shadowed CWD file must not be loaded"),
    )

    async def _fake_reconcile_all(conn: object, doc: object, store: object) -> ReconcileDiff:
        return ReconcileDiff()

    monkeypatch.setattr(inv, "reconcile_all", _fake_reconcile_all)

    pass_ = InventoryReconcilePass()
    with caplog.at_level("WARNING", logger="kdive.reconciler.inventory"):
        assert asyncio.run(pass_.run(_CONN, _store())) == 0
        assert asyncio.run(pass_.run(_CONN, _store())) == 0

    warnings = [r for r in caplog.records if "no longer auto-loaded" in r.getMessage()]
    assert len(warnings) == 1
