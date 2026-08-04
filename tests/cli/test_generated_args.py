"""Protected generated-argument access and generic payload construction."""

from __future__ import annotations

import argparse

import pytest

from kdive.cli.commands.generated_args import (
    GENERATED_ARG_PREFIX,
    local_acknowledgement,
    optional_generated_arg,
    raw_generated_arg,
    required_generated_arg,
)


def _args(**values: object) -> argparse.Namespace:
    return argparse.Namespace(**values)


def _dest(name: str) -> str:
    return f"{GENERATED_ARG_PREFIX}{name}"


def test_raw_generated_arg_reads_only_the_protected_namespace() -> None:
    args = _args(name="routing-value", **{_dest("name"): "tool-value"})

    assert raw_generated_arg(args, "name") == "tool-value"


@pytest.mark.parametrize(("value", "expected_type"), [("system-1", str), (4, int)])
def test_required_generated_arg_returns_a_value_of_the_requested_type(
    value: object, expected_type: type[object]
) -> None:
    args = _args(**{_dest("value"): value})

    assert required_generated_arg(args, "value", expected_type) == value


def test_required_generated_arg_rejects_a_missing_value_with_its_protected_name() -> None:
    with pytest.raises(ValueError, match="genarg_system_id"):
        required_generated_arg(_args(), "system_id", str)


def test_required_generated_arg_rejects_bool_for_an_int() -> None:
    with pytest.raises(ValueError, match="genarg_attempts"):
        required_generated_arg(_args(**{_dest("attempts"): True}), "attempts", int)


def test_optional_generated_arg_returns_none_only_when_absent() -> None:
    assert optional_generated_arg(_args(), "timeout_s", float) is None


def test_optional_generated_arg_rejects_a_wrong_present_type() -> None:
    with pytest.raises(ValueError, match="genarg_timeout_s"):
        optional_generated_arg(_args(**{_dest("timeout_s"): "later"}), "timeout_s", float)


def test_local_acknowledgement_does_not_read_a_generated_value() -> None:
    args = _args(force=False, **{_dest("force"): True})

    assert local_acknowledgement(args, "force") is False


def test_local_acknowledgement_defaults_to_false_when_omitted() -> None:
    assert local_acknowledgement(_args(), "expired") is False
