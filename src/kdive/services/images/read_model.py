"""Application-facing image catalog read-model helpers."""

from __future__ import annotations

from uuid import UUID

from psycopg.cursor_async import AsyncCursor
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from kdive.domain.capacity.state import SystemState
from kdive.domain.catalog.resources import ResourceKind

_TERMINAL_SYSTEM_STATES = (SystemState.TORN_DOWN, SystemState.FAILED)
_TERMINAL_SYSTEM_STATE_VALUES = tuple(state.value for state in _TERMINAL_SYSTEM_STATES)
_LOCAL_LIBVIRT_SECTION = ResourceKind.LOCAL_LIBVIRT.value
_REMOTE_LIBVIRT_SECTION = ResourceKind.REMOTE_LIBVIRT.value


async def image_referenced_by_live_system(cur: AsyncCursor[DictRow], row_id: UUID) -> bool:
    """Return whether a non-terminal System references this image as its base."""
    await cur.execute("SELECT provider, name, volume FROM image_catalog WHERE id = %s", (row_id,))
    image = await cur.fetchone()
    if image is None:
        return False
    probes = [
        Jsonb(
            {
                "provider": {
                    _LOCAL_LIBVIRT_SECTION: {
                        "rootfs": {
                            "kind": "catalog",
                            "provider": image["provider"],
                            "name": image["name"],
                        }
                    }
                }
            }
        )
    ]
    if image["volume"] is not None:
        probes.append(
            Jsonb({"provider": {_REMOTE_LIBVIRT_SECTION: {"base_image_volume": image["volume"]}}})
        )
    for probe in probes:
        await cur.execute(
            "SELECT 1 FROM systems WHERE state <> ALL(%s) AND provisioning_profile @> %s LIMIT 1",
            (list(_TERMINAL_SYSTEM_STATE_VALUES), probe),
        )
        if await cur.fetchone() is not None:
            return True
    return False
