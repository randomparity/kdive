"""Typed row-extraction helpers shared by the inventory reconcile modules.

Each reconcile pass reads ``dict_row`` results whose values are typed ``object`` and
must narrow them to concrete types before constructing its row dataclasses. The same
isinstance-narrowing validators were previously duplicated verbatim across every
reconcile module. A :class:`RowTyper` bound to the table name centralizes them, so a
database row that violates the expected shape fails loudly with one consistent
:class:`InventoryError` naming the table, field, and expected type.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from kdive.inventory.errors import InventoryError


@dataclass(frozen=True, slots=True)
class RowTyper:
    """Narrows ``object``-typed database-row values to concrete types for one table.

    Args:
        table: The entry name embedded in raised :class:`InventoryError` messages
            (e.g. ``"image_catalog"`` or ``"resources"``).
    """

    table: str

    def error(self, field: str, expected: str) -> InventoryError:
        """Build the row-shape failure for ``field`` against an ``expected`` type."""
        return InventoryError(self.table, field, f"database row expected {expected}")

    def uuid(self, row: Mapping[str, object], field: str) -> UUID:
        value = row[field]
        if not isinstance(value, UUID):
            raise self.error(field, "uuid")
        return value

    def string(self, row: Mapping[str, object], field: str) -> str:
        value = row[field]
        if not isinstance(value, str):
            raise self.error(field, "str")
        return value

    def optional_string(self, row: Mapping[str, object], field: str) -> str | None:
        value = row[field]
        if value is not None and not isinstance(value, str):
            raise self.error(field, "str or null")
        return value

    def integer(self, row: Mapping[str, object], field: str) -> int:
        value = row[field]
        if not isinstance(value, int) or isinstance(value, bool):
            raise self.error(field, "int")
        return value

    def boolean(self, row: Mapping[str, object], field: str) -> bool:
        value = row[field]
        if not isinstance(value, bool):
            raise self.error(field, "bool")
        return value

    def optional_datetime(self, row: Mapping[str, object], field: str) -> datetime | None:
        value = row[field]
        if value is not None and not isinstance(value, datetime):
            raise self.error(field, "datetime or null")
        return value

    def string_list(self, row: Mapping[str, object], field: str) -> list[str]:
        value = row[field]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise self.error(field, "list[str]")
        return cast("list[str]", value)


__all__ = ["RowTyper"]
