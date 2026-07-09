"""Cover the RowTyper isinstance-narrowing validators (accept + reject paths)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from kdive.inventory._row_typing import RowTyper
from kdive.inventory.errors import InventoryError

_TYPER = RowTyper("resources")


def test_error_names_table_field_and_expected() -> None:
    err = _TYPER.error("host", "uuid")
    assert isinstance(err, InventoryError)
    assert err.entry == "resources"
    assert err.field == "host"
    assert "database row expected uuid" in str(err)


def test_uuid_accepts_uuid() -> None:
    value = uuid4()
    assert _TYPER.uuid({"id": value}, "id") is value


def test_uuid_rejects_non_uuid() -> None:
    with pytest.raises(InventoryError, match="uuid"):
        _TYPER.uuid({"id": "not-a-uuid"}, "id")


def test_string_accepts_str() -> None:
    assert _TYPER.string({"name": "x"}, "name") == "x"


def test_string_rejects_non_str() -> None:
    with pytest.raises(InventoryError, match="str"):
        _TYPER.string({"name": 5}, "name")


def test_optional_string_accepts_none_and_str() -> None:
    assert _TYPER.optional_string({"label": None}, "label") is None
    assert _TYPER.optional_string({"label": "v"}, "label") == "v"


def test_optional_string_rejects_non_str_non_none() -> None:
    with pytest.raises(InventoryError, match="str or null"):
        _TYPER.optional_string({"label": 7}, "label")


def test_integer_accepts_int() -> None:
    assert _TYPER.integer({"n": 42}, "n") == 42


def test_integer_rejects_non_int() -> None:
    with pytest.raises(InventoryError, match="int"):
        _TYPER.integer({"n": "42"}, "n")


def test_integer_rejects_bool_as_int_trap() -> None:
    # bool is a subclass of int; the validator must reject True/False as an integer
    with pytest.raises(InventoryError, match="int"):
        _TYPER.integer({"n": True}, "n")


def test_boolean_accepts_bool() -> None:
    assert _TYPER.boolean({"flag": True}, "flag") is True


def test_boolean_rejects_non_bool() -> None:
    with pytest.raises(InventoryError, match="bool"):
        _TYPER.boolean({"flag": 1}, "flag")


def test_optional_datetime_accepts_none_and_datetime() -> None:
    now = datetime.now(UTC)
    assert _TYPER.optional_datetime({"ts": None}, "ts") is None
    assert _TYPER.optional_datetime({"ts": now}, "ts") is now


def test_optional_datetime_rejects_non_datetime() -> None:
    with pytest.raises(InventoryError, match="datetime or null"):
        _TYPER.optional_datetime({"ts": "2026-01-01"}, "ts")


def test_string_list_accepts_list_of_str() -> None:
    assert _TYPER.string_list({"tags": ["a", "b"]}, "tags") == ["a", "b"]


def test_string_list_rejects_non_list() -> None:
    with pytest.raises(InventoryError, match=r"list\[str\]"):
        _TYPER.string_list({"tags": "a,b"}, "tags")


def test_string_list_rejects_list_with_non_str_element() -> None:
    with pytest.raises(InventoryError, match=r"list\[str\]"):
        _TYPER.string_list({"tags": ["a", 2]}, "tags")


def test_table_name_appears_in_every_error() -> None:
    typer = RowTyper("resources")
    with pytest.raises(InventoryError) as exc:
        typer.string({"x": 1}, "x")
    assert exc.value.entry == "resources"
