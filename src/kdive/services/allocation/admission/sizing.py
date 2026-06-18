"""DB-backed allocation sizing resolution (ADR-0067)."""

from __future__ import annotations

from psycopg import AsyncConnection

from kdive.db.repositories import SYSTEM_SHAPES
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.shapes import ResolvedSizing, ShapeSizing
from kdive.domain.lifecycle.sizing import MB_PER_GB


async def resolve_shape(conn: AsyncConnection, name: str) -> ShapeSizing:
    """Resolve a shape ``name`` to its sizing tuple from ``system_shapes``.

    Fails closed: a name with no catalog row is a ``configuration_error``, never a silent
    default (ADR-0067). Reads the persisted catalog, never request data.

    Args:
        conn: An async connection to the migrated database.
        name: The shape name to resolve (e.g. ``"medium"``).

    Returns:
        The resolved :class:`~kdive.domain.lifecycle.shapes.ShapeSizing` for ``name``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``name`` has no catalog row.
    """
    shape = await SYSTEM_SHAPES.get(conn, name)
    if shape is None:
        raise CategorizedError(
            f"system shape {name!r} is not in the catalog",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"shape": name},
        )
    return ShapeSizing(
        vcpus=shape.vcpus,
        memory_mb=shape.memory_mb,
        disk_gb=shape.disk_gb,
        pcie_match=shape.pcie_match,
    )


async def resolve_request_sizing(
    conn: AsyncConnection,
    *,
    shape: str | None,
    vcpus: int | None,
    memory_gb: int | None,
    disk_gb: int | None,
) -> ResolvedSizing:
    """Resolve a shape-XOR-custom request to one :class:`ResolvedSizing` (ADR-0067).

    A named ``shape`` resolves through :func:`resolve_shape` (fail-closed on an unknown
    name) and maps ``memory_mb -> memory_gb`` losslessly. A full-custom triple is taken as
    given. The shape-XOR-custom rule is enforced at the request-payload boundary, so this
    fails closed if it ever sees an incomplete custom triple (defence in depth).

    Args:
        conn: An async connection to the migrated database.
        shape: The named shape, or ``None`` for a full-custom request.
        vcpus: Custom vCPU count (required when ``shape`` is ``None``).
        memory_gb: Custom memory in GB (required when ``shape`` is ``None``).
        disk_gb: Custom disk in GB (required when ``shape`` is ``None``).

    Returns:
        The unified :class:`~kdive.domain.lifecycle.shapes.ResolvedSizing`.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an unknown shape or an incomplete
            custom triple.
    """
    if shape is not None:
        sizing = await resolve_shape(conn, shape)
        return ResolvedSizing(
            vcpus=sizing.vcpus,
            memory_gb=sizing.memory_mb // MB_PER_GB,
            disk_gb=sizing.disk_gb,
            pcie_match=sizing.pcie_match,
            shape=shape,
        )
    if vcpus is None or memory_gb is None or disk_gb is None:
        raise CategorizedError(
            "a full-custom request must supply vcpus, memory_gb, and disk_gb",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return ResolvedSizing(vcpus=vcpus, memory_gb=memory_gb, disk_gb=disk_gb)
