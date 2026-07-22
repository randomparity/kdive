"""Focused unit tests for the reconciler inventory pass (``kdive.reconciler.inventory``).

These import ONLY ``kdive.reconciler.inventory`` (never ``kdive.reconciler.loop``), so the
mutated pass code is never executed at import time via loop.py's module-level
``_INVENTORY_PASS = InventoryReconcilePass()`` singleton — which otherwise trips mutmut's
trampoline hit-recorder on the frozen-importlib pseudo-filename and aborts the baseline.

``reconcile_all`` and ``load_inventory_optional`` are stubbed so the pass's file-hash cache,
absent-file no-op, drift-repair-every-pass, OSError deferral, and CWD-shadow warn-once logic are
exercised without a Postgres fixture. End-to-end DB coverage lives in
``tests/integration/test_reconcile_inventory.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from psycopg import AsyncConnection

from kdive.inventory.errors import InventoryError
from kdive.inventory.model import InventoryDoc
from kdive.inventory.reconcile.images import ImageHeadStore
from kdive.inventory.reconcile.records import ReconcileDiff, ReconcileRecord
from kdive.reconciler import inventory as inv
from kdive.reconciler.inventory import InventoryReconcilePass, _changes

# Opaque sentinels: the stubbed reconcile_all/loader never touch the connection, and the store
# is only forwarded, so the real protocol behaviour is never exercised here.
_CONN = cast("AsyncConnection[Any]", object())


class _FakeStore:
    """A structural ImageHeadStore stand-in; the stubbed reconcile_all ignores it."""

    def head_present(self, key: str) -> bool:  # pragma: no cover - never called by stubs
        return False

    def list_image_objects(self) -> list[Any]:  # pragma: no cover
        return []


def _store() -> ImageHeadStore:
    return cast(ImageHeadStore, _FakeStore())


def _rec(name: str) -> ReconcileRecord:
    return ReconcileRecord(name=name, entry=name)


def _doc() -> InventoryDoc:
    return InventoryDoc(schema_version=2)


def _stub_reconcile_all(
    monkeypatch: pytest.MonkeyPatch, diff: ReconcileDiff, calls: list[Any]
) -> None:
    async def _fake(conn: object, doc: object, store: object) -> ReconcileDiff:
        calls.append((conn, doc, store))
        return diff

    monkeypatch.setattr(inv, "reconcile_all", _fake)


# --- _changes ------------------------------------------------------------------------


def test_changes_sums_created_updated_pruned_cordoned_only() -> None:
    # Distinct per-category counts (1/2/4/8) so dropping or negating any term yields a unique,
    # detectable total; `warned` is deliberately excluded from the change count.
    diff = ReconcileDiff(
        created=[_rec("a")],
        updated=[_rec("b"), _rec("c")],
        pruned=[_rec(f"p{i}") for i in range(4)],
        cordoned=[_rec(f"c{i}") for i in range(8)],
        warned=[_rec(f"w{i}") for i in range(16)],
    )
    assert _changes(diff) == 15  # 1 + 2 + 4 + 8, warned (16) not counted


# --- run(): present / absent file ----------------------------------------------------


def test_run_reconciles_present_file_and_returns_change_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "systems.toml"
    path.write_text("schema_version = 2\n")
    monkeypatch.setattr(inv, "systems_toml_path", lambda: path)
    doc = _doc()

    def _load(p: Path) -> InventoryDoc:
        assert p == path  # the resolved file path is parsed, not None or some other path
        return doc

    monkeypatch.setattr(inv, "load_inventory_optional", _load)
    calls: list[Any] = []
    _stub_reconcile_all(monkeypatch, ReconcileDiff(created=[_rec("x"), _rec("y")]), calls)

    with caplog.at_level("WARNING", logger="kdive.reconciler.inventory"):
        result = asyncio.run(InventoryReconcilePass().run(_CONN, _store()))

    assert result == 2  # the diff's two created rows
    assert len(calls) == 1  # reconcile ran exactly once
    assert calls[0][1] is doc  # the parsed doc was forwarded, not a fresh/empty one
    # A resolved, existing file is NOT shadowed: the CWD-shadow warning must stay silent (the
    # branch is `not warned AND shadowed`, never `or`).
    assert not [r for r in caplog.records if "no longer auto-loaded" in r.getMessage()]


def test_run_absent_file_is_quiet_noop_and_skips_reconcile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(inv, "systems_toml_path", lambda: tmp_path / "absent.toml")
    monkeypatch.setattr(
        inv,
        "load_inventory_optional",
        lambda p: pytest.fail("loader must not be called for an absent file"),
    )
    calls: list[Any] = []
    _stub_reconcile_all(monkeypatch, ReconcileDiff(created=[_rec("x")]), calls)

    result = asyncio.run(InventoryReconcilePass().run(_CONN, _store()))

    assert result == 0  # absent file → 0 changes
    assert calls == []  # reconcile_all was never reached


# --- run(): hash cache vs. drift-repair-every-pass -----------------------------------


def test_unchanged_file_skips_reparse_but_still_reconciles_each_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ADR-0021: the content-hash cache may skip only the parse step; the reconcile-against-DB
    # step must run EVERY pass so DB drift is repaired even when the file is unchanged.
    path = tmp_path / "systems.toml"
    path.write_text("schema_version = 2\n")
    monkeypatch.setattr(inv, "systems_toml_path", lambda: path)
    parse_calls: list[Path] = []

    def _spy_load(p: Path) -> InventoryDoc:
        assert p == path  # the resolved file is parsed, not None
        parse_calls.append(p)
        return _doc()

    monkeypatch.setattr(inv, "load_inventory_optional", _spy_load)
    recon_calls: list[Any] = []
    _stub_reconcile_all(monkeypatch, ReconcileDiff(created=[_rec("z")]), recon_calls)

    pass_ = InventoryReconcilePass()
    first = asyncio.run(pass_.run(_CONN, _store()))
    second = asyncio.run(pass_.run(_CONN, _store()))

    assert first == second == 1
    assert len(parse_calls) == 1  # second pass hit the hash cache, did not re-parse
    assert len(recon_calls) == 2  # reconcile ran BOTH passes (drift repair not hash-gated)


def test_changed_file_is_reparsed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "systems.toml"
    path.write_text("schema_version = 2\n")
    monkeypatch.setattr(inv, "systems_toml_path", lambda: path)
    parse_calls: list[Path] = []

    def _spy_load(p: Path) -> InventoryDoc:
        parse_calls.append(p)
        return _doc()

    monkeypatch.setattr(inv, "load_inventory_optional", _spy_load)
    _stub_reconcile_all(monkeypatch, ReconcileDiff(), [])

    pass_ = InventoryReconcilePass()
    asyncio.run(pass_.run(_CONN, _store()))
    path.write_text("schema_version = 2\n# changed bytes\n")  # different hash
    asyncio.run(pass_.run(_CONN, _store()))

    assert len(parse_calls) == 2  # a changed hash forces a re-parse


def test_absent_file_clears_a_prior_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A FileNotFoundError resets the cache, so a later re-creation with the SAME bytes re-parses
    # rather than serving a stale cached doc.
    path = tmp_path / "systems.toml"
    body = "schema_version = 2\n"
    path.write_text(body)
    monkeypatch.setattr(inv, "systems_toml_path", lambda: path)
    parse_calls: list[Path] = []

    def _spy_load(p: Path) -> InventoryDoc:
        parse_calls.append(p)
        return _doc()

    monkeypatch.setattr(inv, "load_inventory_optional", _spy_load)
    _stub_reconcile_all(monkeypatch, ReconcileDiff(), [])

    pass_ = InventoryReconcilePass()
    asyncio.run(pass_.run(_CONN, _store()))  # parse #1, caches by hash
    path.unlink()
    assert asyncio.run(pass_.run(_CONN, _store())) == 0  # absent → reset cache
    path.write_text(body)  # identical bytes → same hash as the cleared entry
    asyncio.run(pass_.run(_CONN, _store()))  # parse #2 because the cache was reset

    assert len(parse_calls) == 2


# --- run(): present-but-unreadable defers to the loader (OSError branch) --------------


def test_unreadable_path_defers_to_loader_and_reraises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    as_dir = tmp_path / "systems.toml"
    as_dir.mkdir()  # read_bytes raises IsADirectoryError (an OSError, not FileNotFoundError)
    monkeypatch.setattr(inv, "systems_toml_path", lambda: as_dir)

    def _boom_load(p: Path) -> InventoryDoc:
        assert p == as_dir  # the OSError branch defers to the loader with the real path, not None
        raise InventoryError("systems.toml", "root", "present but unreadable")

    monkeypatch.setattr(inv, "load_inventory_optional", _boom_load)
    _stub_reconcile_all(monkeypatch, ReconcileDiff(), [])

    with pytest.raises(InventoryError):
        asyncio.run(InventoryReconcilePass().run(_CONN, _store()))


def test_malformed_present_file_raises_inventory_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "systems.toml"
    path.write_text("schema_version = 2\n")
    monkeypatch.setattr(inv, "systems_toml_path", lambda: path)

    def _boom_load(p: Path) -> InventoryDoc:
        raise InventoryError("systems.toml", "root", "malformed")

    monkeypatch.setattr(inv, "load_inventory_optional", _boom_load)
    _stub_reconcile_all(monkeypatch, ReconcileDiff(), [])

    with pytest.raises(InventoryError):
        asyncio.run(InventoryReconcilePass().run(_CONN, _store()))


# --- make_repair passthrough ---------------------------------------------------------


def test_make_repair_forwards_conn_and_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "systems.toml"
    path.write_text("schema_version = 2\n")
    monkeypatch.setattr(inv, "systems_toml_path", lambda: path)
    monkeypatch.setattr(inv, "load_inventory_optional", lambda p: _doc())
    calls: list[Any] = []
    _stub_reconcile_all(monkeypatch, ReconcileDiff(created=[_rec("m")]), calls)

    store = _store()
    repair = InventoryReconcilePass().make_repair(store)
    result = asyncio.run(repair(_CONN))

    assert result == 1
    assert calls[0][0] is _CONN and calls[0][2] is store  # both forwarded unchanged
