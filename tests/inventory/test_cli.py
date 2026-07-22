"""Tests for the ``kdive reconcile-systems`` CLI (M2.6 #391, plan Task 1.6).

The CLI runs one ``reconcile_images`` pass against the pool, prints the ``ReconcileDiff``,
and exits non-zero on an ``InventoryError``. The reconcile core (:func:`reconcile_systems`)
takes an injected pool + store so these tests drive it against a disposable migrated
Postgres without standing up the whole process; the env-resolved path is covered by the
absent-default no-op test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.inventory.cli import _print_diff, reconcile_systems
from kdive.inventory.reconcile.records import ReconcileDiff, ReconcileRecord

# `migrated_url` is provided by tests/inventory/conftest.py (re-exported from tests.db.conftest),
# resolved by pytest at call time — no import here (avoids the F811 fixture-shadow).


def _fake_put(request: ArtifactWriteRequest) -> StoredArtifact:
    return StoredArtifact(
        key=request.key(),
        etag="etag",
        sensitivity=request.sensitivity,
        retention_class=request.retention_class,
    )


class _FakeImageStore:
    """A narrow head-only object-store stand-in for the reconcile pass."""

    def head_present(self, key: str) -> bool:
        return False

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        return _fake_put(request)


_STAGED_IMAGE = (
    "schema_version = 2\n"
    "[[image]]\n"
    'provider = "remote-libvirt"\n'
    'name = "cli-base"\n'
    'arch = "x86_64"\n'
    'format = "qcow2"\n'
    'root_device = "/dev/vda"\n'
    'visibility = "public"\n'
    "[image.source]\n"
    'kind = "staged"\n'
    'volume = "cli-base.qcow2"\n'
)


def test_reconcile_systems_happy_path_creates_row(
    migrated_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _run() -> None:
        path = tmp_path / "systems.toml"
        path.write_text(_STAGED_IMAGE)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            code = await reconcile_systems(path, pool=pool, store=_FakeImageStore())
            assert code == 0
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM image_catalog WHERE name = 'cli-base'")
                row = await cur.fetchone()
        assert row is not None and row["state"] == "registered"
        out = capsys.readouterr().out
        assert "cli-base" in out
        assert "created" in out

    asyncio.run(_run())


def test_reconcile_systems_missing_explicit_path_exits_nonzero(
    migrated_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An explicit --path to a missing file is an operator error (load_inventory raises
    # InventoryError); the CLI reports it and exits non-zero, never a traceback.
    async def _run() -> None:
        missing = tmp_path / "absent.toml"
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            code = await reconcile_systems(missing, pool=pool, store=_FakeImageStore())
        assert code != 0
        err = capsys.readouterr().err
        assert "absent.toml" in err

    asyncio.run(_run())


def test_reconcile_systems_malformed_file_exits_nonzero(
    migrated_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _run() -> None:
        path = tmp_path / "systems.toml"
        path.write_text("schema_version = 2\n[[image]\n")  # malformed TOML
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            code = await reconcile_systems(path, pool=pool, store=_FakeImageStore())
        assert code != 0
        err = capsys.readouterr().err
        assert "systems.toml" in err

    asyncio.run(_run())


def test_reconcile_systems_absent_default_is_quiet_no_op(
    migrated_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # With no --path and an absent default file, the CLI is a quiet no-op (exit 0): it must
    # not prune every config row by feeding an empty inventory to reconcile_images.
    async def _run() -> None:
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "does-not-exist.toml"))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            code = await reconcile_systems(None, pool=pool, store=_FakeImageStore())
        assert code == 0
        # The no-op emits its exact operator-facing line (not an empty/garbled message).
        assert capsys.readouterr().out.strip() == "no systems.toml present; nothing to reconcile"

    asyncio.run(_run())


def test_print_diff_prints_every_category_header_and_record_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # _print_diff renders one "<label>: <count>" header per category and one indented line
    # per record, appending an em-dash detail suffix only when the record carries a detail.
    diff = ReconcileDiff(
        created=[ReconcileRecord(name="a", entry="img-a")],
        updated=[ReconcileRecord(name="b", entry="img-b", detail="bumped")],
        pruned=[ReconcileRecord(name="c", entry="img-c")],
        cordoned=[ReconcileRecord(name="d", entry="img-d", detail="orphaned")],
        warned=[ReconcileRecord(name="e", entry="img-e", detail="missing size")],
    )
    _print_diff(diff)
    lines = capsys.readouterr().out.splitlines()
    # Exact header lines pin every label spelling + the "<label>: <count>" format.
    assert "created: 1" in lines
    assert "updated: 1" in lines
    assert "pruned: 1" in lines
    assert "cordoned: 1" in lines
    assert "warned: 1" in lines
    # A record with no detail renders exactly "  <entry>" (no suffix, no sentinel).
    assert "  img-a" in lines
    assert "  img-c" in lines
    # A record with a detail appends " — <detail>".
    assert "  img-b — bumped" in lines
    assert "  img-d — orphaned" in lines
    assert "  img-e — missing size" in lines


def test_reconcile_systems_store_unreachable_propagates(migrated_url: str, tmp_path: Path) -> None:
    # A store/infra failure during the pass is not an InventoryError; it propagates so the
    # operator sees the real cause rather than a silent success.
    class _UnreachableStore:
        def head_present(self, key: str) -> bool:
            raise CategorizedError("store down", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

        def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
            return _fake_put(request)

    async def _run() -> None:
        path = tmp_path / "systems.toml"
        # An s3 source with a digest forces a HEAD, exercising the store path.
        path.write_text(
            "schema_version = 2\n"
            "[[image]]\n"
            'provider = "local-libvirt"\n'
            'name = "s3img"\n'
            'arch = "x86_64"\n'
            'format = "qcow2"\n'
            'root_device = "/dev/vda"\n'
            'visibility = "public"\n'
            "[image.source]\n"
            'kind = "s3"\n'
            'object_key = "images/local-libvirt/s3img/x86_64.qcow2"\n'
            'digest = "sha256:beef"\n'
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            # reconcile_images degrades a store-unreachable HEAD to "defined" + warn, so this
            # is a success path (exit 0), not a propagated error — the row stays defined.
            code = await reconcile_systems(path, pool=pool, store=_UnreachableStore())
        assert code == 0

    asyncio.run(_run())
