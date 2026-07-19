"""Direct unit tests for the image-catalog read-model live-reference probe."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import UUID, uuid4

from psycopg.cursor_async import AsyncCursor
from psycopg.rows import DictRow

from kdive.images.cataloging.read_model import image_referenced_by_live_system


class _FakeCursor:
    """An async cursor that replays a queued sequence of ``fetchone`` results."""

    def __init__(self, fetch_results: list[object]) -> None:
        self._results = list(fetch_results)
        self.executed: list[tuple[str, object]] = []

    async def execute(self, sql: str, params: object) -> None:
        self.executed.append((sql, params))

    async def fetchone(self) -> object:
        return self._results.pop(0)


def _run(cursor: _FakeCursor, row_id: UUID) -> bool:
    return asyncio.run(image_referenced_by_live_system(cast(AsyncCursor[DictRow], cursor), row_id))


def test_absent_image_is_not_referenced() -> None:
    cursor = _FakeCursor([None])
    assert _run(cursor, uuid4()) is False
    assert len(cursor.executed) == 1


def test_image_without_live_system_is_not_referenced() -> None:
    image = {"provider": "libvirt", "name": "fedora", "volume": None}
    cursor = _FakeCursor([image, None])
    assert _run(cursor, uuid4()) is False
    # No volume -> only the local-libvirt rootfs probe is issued (image read + one probe).
    assert len(cursor.executed) == 2


def test_live_system_via_local_libvirt_rootfs_is_referenced() -> None:
    image = {"provider": "libvirt", "name": "fedora", "volume": None}
    cursor = _FakeCursor([image, {"?column?": 1}])
    assert _run(cursor, uuid4()) is True


def test_volume_backed_image_probes_remote_libvirt_base_volume() -> None:
    image = {"provider": "libvirt", "name": "fedora", "volume": "vol-1"}
    # local probe misses, remote base-volume probe hits.
    cursor = _FakeCursor([image, None, {"?column?": 1}])
    assert _run(cursor, uuid4()) is True
    assert len(cursor.executed) == 3
