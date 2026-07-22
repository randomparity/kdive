"""DB-backed allocation sizing resolution tests (ADR-0067)."""

from __future__ import annotations

import asyncio

import psycopg
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.shapes import ResolvedSizing, ShapeSizing
from kdive.services.allocation.admission.sizing import resolve_request_sizing, resolve_shape

# The four shapes migration 0013 seeds (ADR-0067), with their sizing tuples.
_SEED_SHAPES = {
    "small": (1, 1024, 10),
    "medium": (2, 4096, 20),
    "large": (4, 8192, 40),
    "max": (8, 16384, 80),
}


@pytest.mark.parametrize("name", sorted(_SEED_SHAPES))
def test_resolve_shape_returns_seeded_tuple(migrated_url: str, name: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            sizing = await resolve_shape(conn, name)
        vcpus, memory_mb, disk_gb = _SEED_SHAPES[name]
        assert sizing == ShapeSizing(
            vcpus=vcpus, memory_mb=memory_mb, disk_gb=disk_gb, pcie_match=None
        )

    asyncio.run(_run())


def test_resolve_shape_unknown_name_fails_closed(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            try:
                await resolve_shape(conn, "no-such-shape")
                raise AssertionError("expected CategorizedError")
            except CategorizedError as exc:
                assert exc.category is ErrorCategory.CONFIGURATION_ERROR
                assert exc.details["shape"] == "no-such-shape"
                assert str(exc) == "system shape 'no-such-shape' is not in the catalog"

    asyncio.run(_run())


def test_resolve_shape_carries_pcie_match(migrated_url: str) -> None:
    """A shape row with a non-NULL pcie_match resolves it onto the sizing tuple."""

    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            await conn.execute(
                "INSERT INTO system_shapes (name, vcpus, memory_mb, disk_gb, pcie_match) "
                "VALUES (%s, %s, %s, %s, %s)",
                ("gpu-shape", 4, 8192, 40, "vendor:10de"),
            )
            await conn.commit()
            sizing = await resolve_shape(conn, "gpu-shape")
        assert sizing == ShapeSizing(vcpus=4, memory_mb=8192, disk_gb=40, pcie_match="vendor:10de")

    asyncio.run(_run())


def test_resolve_request_sizing_maps_shape_to_priced_tuple(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            sizing = await resolve_request_sizing(
                conn, shape="large", vcpus=None, memory_gb=None, disk_gb=None
            )
        # large = 4 vcpu / 8192 MB / 40 GB; memory_mb -> memory_gb is lossless (// 1024).
        assert sizing == ResolvedSizing(
            vcpus=4, memory_gb=8, disk_gb=40, pcie_match=None, shape="large"
        )

    asyncio.run(_run())


def test_resolve_request_sizing_shape_carries_pcie_match(migrated_url: str) -> None:
    """A named shape's pcie_match is carried through onto the resolved sizing."""

    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            await conn.execute(
                "INSERT INTO system_shapes (name, vcpus, memory_mb, disk_gb, pcie_match) "
                "VALUES (%s, %s, %s, %s, %s)",
                ("gpu-shape", 4, 8192, 40, "vendor:10de"),
            )
            await conn.commit()
            sizing = await resolve_request_sizing(
                conn, shape="gpu-shape", vcpus=None, memory_gb=None, disk_gb=None
            )
        assert sizing == ResolvedSizing(
            vcpus=4, memory_gb=8, disk_gb=40, pcie_match="vendor:10de", shape="gpu-shape"
        )

    asyncio.run(_run())


def test_resolve_request_sizing_passes_custom_triple_through(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            sizing = await resolve_request_sizing(
                conn, shape=None, vcpus=3, memory_gb=6, disk_gb=30
            )
        assert sizing == ResolvedSizing(
            vcpus=3, memory_gb=6, disk_gb=30, pcie_match=None, shape=None
        )

    asyncio.run(_run())


def test_resolve_request_sizing_unknown_shape_fails_closed(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as exc:
                await resolve_request_sizing(
                    conn, shape="nope", vcpus=None, memory_gb=None, disk_gb=None
                )
            assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("vcpus", "memory_gb", "disk_gb"),
    [
        (3, 6, None),  # missing disk_gb
        (None, 6, 30),  # missing vcpus
        (3, None, 30),  # missing memory_gb
    ],
)
def test_resolve_request_sizing_incomplete_custom_fails_closed(
    migrated_url: str, vcpus: int | None, memory_gb: int | None, disk_gb: int | None
) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as exc:
                await resolve_request_sizing(
                    conn, shape=None, vcpus=vcpus, memory_gb=memory_gb, disk_gb=disk_gb
                )
            assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert str(exc.value) == (
                "a full-custom request must supply vcpus, memory_gb, and disk_gb"
            )

    asyncio.run(_run())
