"""Protected access to parser values owned by generated CLI descriptors."""

from __future__ import annotations

import argparse
import json
from typing import cast

from kdive.cli.commands.verb_spec import GeneratedVerb

__all__ = [
    "GENERATED_ARG_PREFIX",
    "assemble_generated_payload",
    "local_acknowledgement",
    "optional_generated_arg",
    "raw_generated_arg",
    "required_generated_arg",
]


#: Generated-verb values use this prefix so MCP parameter names cannot overwrite parser routing
#: fields such as ``command``, ``subcommand``, or ``json``.
GENERATED_ARG_PREFIX = "genarg_"

_MISSING = object()


def _protected_name(name: str) -> str:
    return f"{GENERATED_ARG_PREFIX}{name}"


def _protected_value(args: argparse.Namespace, name: str) -> object:
    return cast(object, vars(args).get(_protected_name(name), _MISSING))


def raw_generated_arg(args: argparse.Namespace, name: str) -> object | None:
    """Return a generated value by its protected parser destination, or ``None`` when absent."""
    value = _protected_value(args, name)
    return None if value is _MISSING else value


def _matches_expected_type(value: object, expected_type: type[object]) -> bool:
    return type(value) is int if expected_type is int else isinstance(value, expected_type)


def _invalid_type_error(name: str, expected_type: type[object], value: object) -> ValueError:
    return ValueError(
        f"invalid generated argument {_protected_name(name)!r}: expected "
        f"{expected_type.__name__}, got {type(value).__name__}"
    )


def required_generated_arg[Value](
    args: argparse.Namespace, name: str, expected_type: type[Value]
) -> Value:
    """Return a required generated value after validating its parser-contract type.

    Raises:
        ValueError: When the protected destination is absent, null, or has an unexpected type.
    """
    value = _protected_value(args, name)
    if value is _MISSING:
        raise ValueError(f"missing generated argument {_protected_name(name)!r}")
    expected = cast(type[object], expected_type)
    if value is None or not _matches_expected_type(value, expected):
        raise _invalid_type_error(name, expected, value)
    return cast(Value, value)


def optional_generated_arg[Value](
    args: argparse.Namespace, name: str, expected_type: type[Value]
) -> Value | None:
    """Return an optional generated value after validating its parser-contract type.

    Raises:
        ValueError: When a present protected destination has an unexpected type.
    """
    value = _protected_value(args, name)
    if value is _MISSING or value is None:
        return None
    expected = cast(type[object], expected_type)
    if not _matches_expected_type(value, expected):
        raise _invalid_type_error(name, expected, value)
    return cast(Value, value)


def local_acknowledgement(args: argparse.Namespace, name: str) -> bool:
    """Return a parser-local acknowledgement flag without consulting generated values.

    Raises:
        ValueError: When the local parser field is present but not a boolean.
    """
    value = cast(object, vars(args).get(name, False))
    if type(value) is not bool:
        raise ValueError(
            f"invalid local acknowledgement {name!r}: expected bool, got {type(value).__name__}"
        )
    return value


def assemble_generated_payload(verb: GeneratedVerb, args: argparse.Namespace) -> dict[str, object]:
    """Build a descriptor-owned MCP payload from protected generated parser values.

    ``store_true`` values are omitted unless set; all other scalar values are omitted only when
    absent. JSON-container values are parsed after parser validation. ``unwrap_request`` verbs
    wrap a non-empty body under ``request``.
    """
    body: dict[str, object] = {}
    for flag in verb.flags:
        value = raw_generated_arg(args, flag.dest)
        if flag.action == "store_true":
            if value:
                body[flag.dest] = True
        elif value is not None:
            body[flag.dest] = value
    for param in verb.json_params:
        raw = raw_generated_arg(args, f"{param}_json")
        if raw is not None:
            if not isinstance(raw, str):
                raise _invalid_type_error(f"{param}_json", str, raw)
            body[param] = cast(object, json.loads(raw))
    if verb.unwrap_request:
        return {"request": body} if body else {}
    return body
