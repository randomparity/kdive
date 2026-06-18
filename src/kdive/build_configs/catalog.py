"""Build-config catalog repository (ADR-0096): name -> sha256-verified fragment bytes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from psycopg import AsyncConnection, Connection
from psycopg.rows import dict_row

from kdive.domain.errors import CategorizedError, ErrorCategory

_SELECT = (
    "SELECT name, object_key, sha256, description, source "
    "FROM build_config_catalog WHERE name = %(name)s"
)


@dataclass(frozen=True)
class BuildConfigEntry:
    name: str
    object_key: str
    sha256: str
    description: str
    source: str

    def verify_bytes(self, data: bytes) -> None:
        """Raise INFRASTRUCTURE_FAILURE if ``data`` does not hash to this row's ``sha256``.

        Args:
            data: The raw bytes to verify against the stored digest.

        Raises:
            CategorizedError: INFRASTRUCTURE_FAILURE when the sha256 does not match.
        """
        actual = hashlib.sha256(data).hexdigest()
        if actual != self.sha256:
            raise CategorizedError(
                "build-config object bytes do not match the catalog sha256",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"name": self.name},
            )


def parse_build_config_row(row: Mapping[str, object]) -> BuildConfigEntry:
    return BuildConfigEntry(
        name=_required_str(row, "name"),
        object_key=_required_str(row, "object_key"),
        sha256=_required_str(row, "sha256"),
        description=_required_str(row, "description"),
        source=_required_str(row, "source"),
    )


def _required_str(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise CategorizedError(
            "build-config catalog row has an invalid column",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"column": key},
        )
    return value


async def get_build_config(conn: AsyncConnection, name: str) -> BuildConfigEntry | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_SELECT, {"name": name})
        row = await cur.fetchone()
    return parse_build_config_row(row) if row is not None else None


def get_build_config_sync(conn: Connection, name: str) -> BuildConfigEntry | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT, {"name": name})
        row = cur.fetchone()
    return parse_build_config_row(row) if row is not None else None


async def upsert_operator_build_config(
    conn: AsyncConnection, name: str, object_key: str, sha256: str, description: str
) -> None:
    """Upsert an operator-published fragment row (``source='operator'``), unconditionally.

    An empty ``description`` preserves the row's prior description instead of blanking it, so
    re-publishing bytes without a description keeps the seed's text (ADR-0119).

    Args:
        conn: An open async psycopg connection.
        name: The fragment catalog name.
        object_key: The reserved object-store key the bytes were published to.
        sha256: The hex digest of the published bytes.
        description: A human label; an empty string preserves the prior description.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO build_config_catalog (name, object_key, sha256, description, source) "
            "VALUES (%(name)s, %(object_key)s, %(sha256)s, %(description)s, 'operator') "
            "ON CONFLICT (name) DO UPDATE SET "
            "object_key = EXCLUDED.object_key, sha256 = EXCLUDED.sha256, "
            "description = COALESCE(NULLIF(EXCLUDED.description, ''), "
            "build_config_catalog.description, ''), "
            "source = 'operator', updated_at = now()",
            {"name": name, "object_key": object_key, "sha256": sha256, "description": description},
        )


async def upsert_seed_build_config(
    conn: AsyncConnection, name: str, object_key: str, sha256: str, description: str
) -> None:
    """Upsert a seed-published fragment row (``source='seed'``), guarded against operator rows.

    The ``WHERE build_config_catalog.source = 'seed'`` conflict guard makes the database refuse
    to overwrite an operator override, so a later ``migrate`` never clobbers it (ADR-0119).

    Args:
        conn: An open async psycopg connection.
        name: The fragment catalog name.
        object_key: The reserved object-store key the packaged bytes were published to.
        sha256: The hex digest of the packaged bytes.
        description: The packaged fragment's description.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO build_config_catalog (name, object_key, sha256, description, source) "
            "VALUES (%(name)s, %(object_key)s, %(sha256)s, %(description)s, 'seed') "
            "ON CONFLICT (name) DO UPDATE SET "
            "object_key = EXCLUDED.object_key, sha256 = EXCLUDED.sha256, "
            "description = EXCLUDED.description, source = 'seed', updated_at = now() "
            "WHERE build_config_catalog.source = 'seed'",
            {"name": name, "object_key": object_key, "sha256": sha256, "description": description},
        )


async def read_build_config_provenance(
    conn: AsyncConnection, name: str
) -> tuple[str, str, str] | None:
    """Return ``(sha256, source, description)`` for ``name``, or ``None`` if absent.

    The inventory reconcile pass uses this for change-detection (sha256 + description) and
    drift attribution (source), and the seed reuses it for its source-aware skip (ADR-0122).

    Args:
        conn: An open async psycopg connection.
        name: The fragment catalog name.

    Returns:
        The ``(sha256, source, description)`` triple, or ``None`` when no row exists.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT sha256, source, description FROM build_config_catalog WHERE name = %(name)s",
            {"name": name},
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return (str(row["sha256"]), str(row["source"]), str(row["description"]))


async def upsert_config_build_config(
    conn: AsyncConnection, name: str, object_key: str, sha256: str, description: str
) -> None:
    """Upsert a config-declared fragment row (``source='config'``), unconditionally (ADR-0122).

    The ``systems.toml`` file is authoritative, so this clobbers a ``seed`` or ``operator`` row
    AND writes ``description`` **verbatim** (the file fully specifies the fragment each
    reconcile). It deliberately does NOT use the ``COALESCE``-preserve pattern the operator
    writer uses: that pattern would make a file declaring an empty description un-converge
    against the reconcile pass's ``(sha256, source, description)`` change-detection key (the
    stored description would never blank, so the pass would re-assert every cycle). Verbatim
    keeps the pass idempotent.

    Args:
        conn: An open async psycopg connection.
        name: The fragment catalog name.
        object_key: The reserved object-store key the bytes were published to.
        sha256: The hex digest of the published bytes.
        description: The fragment's description, written verbatim.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO build_config_catalog (name, object_key, sha256, description, source) "
            "VALUES (%(name)s, %(object_key)s, %(sha256)s, %(description)s, 'config') "
            "ON CONFLICT (name) DO UPDATE SET "
            "object_key = EXCLUDED.object_key, sha256 = EXCLUDED.sha256, "
            "description = EXCLUDED.description, source = 'config', updated_at = now()",
            {"name": name, "object_key": object_key, "sha256": sha256, "description": description},
        )
