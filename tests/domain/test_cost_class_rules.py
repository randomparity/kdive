"""Unit tests for the neutral cost-class name/coeff rule (ADR-0115 §1)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kdive.domain.cost_class_rules import parse_positive_coeff, validate_cost_class_name


def test_valid_name_returned_unchanged() -> None:
    assert validate_cost_class_name("remote") == "remote"


@pytest.mark.parametrize("bad", ["", "   ", "\t"])
def test_blank_name_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="non-blank"):
        validate_cost_class_name(bad)


@pytest.mark.parametrize("value", ["2.5", 2.5, 1, Decimal("0.25")])
def test_positive_coeff_parsed_to_decimal(value: object) -> None:
    parsed = parse_positive_coeff(value)
    assert isinstance(parsed, Decimal)
    assert parsed > 0


def test_coeff_uses_string_construction_no_float_drift() -> None:
    # Decimal(str(0.1)) == Decimal("0.1"), not the binary-float expansion.
    assert parse_positive_coeff(0.1) == Decimal("0.1")


@pytest.mark.parametrize("bad", [0, -1, "0", "-2.5"])
def test_non_positive_coeff_rejected(bad: object) -> None:
    with pytest.raises(ValueError, match="> 0"):
        parse_positive_coeff(bad)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", float("nan"), float("inf")])
def test_non_finite_coeff_rejected(bad: object) -> None:
    with pytest.raises(ValueError):
        parse_positive_coeff(bad)


@pytest.mark.parametrize("bad", ["abc", None, object()])
def test_non_numeric_coeff_rejected(bad: object) -> None:
    with pytest.raises(ValueError, match="not a number"):
        parse_positive_coeff(bad)
