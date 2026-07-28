"""Generator behavior + drift guard for the committed ``kdivectl`` verb descriptors (#1447).

The drift guard (``test_committed_module_is_in_sync`` / ``test_every_live_tool_is_covered``)
is what makes adding a server tool without regenerating fail ``just ci``. The remaining tests
pin the pure schema -> descriptor transform against synthetic tool schemas, so they need no
live app build.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from kdive.cli.commands._generated_verbs import GENERATED_VERBS
from kdive.cli.commands.verb_spec import GeneratedFlag, GeneratedVerb
from kdive.cli.reserved_flags import RESERVED_CLI_FLAGS
from scripts import gen_cli_verbs as gen


@dataclass(frozen=True)
class _Ann:
    readOnlyHint: bool = False
    destructiveHint: bool = False


@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    annotations: _Ann


def _tool(name: str, params: dict[str, Any], **kw: Any) -> _Tool:
    return _Tool(name, kw.get("description", "A tool."), params, kw.get("annotations", _Ann()))


# --- Descriptor types ----------------------------------------------------------------------


def test_generated_flag_defaults() -> None:
    flag = GeneratedFlag(name="--id", dest="id", required=True, help="Id.")
    assert (flag.arg_type, flag.action, flag.choices) == (None, None, ())


def test_generated_verb_defaults() -> None:
    verb = GeneratedVerb(
        group="demo", sub="get", tool="demo.get", read_only=True, destructive=False
    )
    assert (verb.help, verb.unwrap_request, verb.flags, verb.json_params) == ("", False, (), ())


# --- Drift guard: the committed module tracks the live registry -----------------------------


def test_committed_module_is_in_sync() -> None:
    """The committed descriptor module equals a fresh generation (drift guard, #1447)."""
    assert gen.check() == 0


def test_every_live_tool_is_covered() -> None:
    """Every registered tool has exactly one descriptor; no stale or missing entries."""
    live = {t.name for t in gen._registry_tools()}
    covered = [v.tool for v in GENERATED_VERBS]
    assert sorted(covered) == sorted(set(covered)), "duplicate tool descriptor"
    assert set(covered) == live


def test_check_detects_a_stale_committed_module(tmp_path) -> None:
    """A committed module that no longer matches a fresh generation fails the check."""
    stale = tmp_path / "_generated_verbs.py"
    stale.write_text(gen.build_module() + "\n# manual drift\n", encoding="utf-8")
    assert gen.check(stale) == 1


def test_check_reports_missing_committed_module(tmp_path) -> None:
    assert gen.check(tmp_path / "absent.py") == 1


# --- Pure transform ------------------------------------------------------------------------


def test_request_wrapper_unwraps_to_flat_scalar_flags() -> None:
    tool = _tool(
        "demo.list",
        {
            "properties": {
                "request": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "anyOf": [
                                        {"enum": ["a", "b"], "type": "string"},
                                        {"type": "null"},
                                    ],
                                    "description": "Kind filter.",
                                },
                                "limit": {"type": "integer", "description": "Rows."},
                            },
                            "required": [],
                        },
                        {"type": "null"},
                    ]
                }
            }
        },
        annotations=_Ann(readOnlyHint=True),
    )
    verb = gen._verb_for(tool)
    assert verb.unwrap_request is True
    assert (verb.group, verb.sub) == ("demo", "list")
    assert verb.read_only is True
    flags = {f.dest: f for f in verb.flags}
    assert flags["kind"].choices == ("a", "b") and flags["kind"].arg_type == "str"
    assert flags["kind"].name == "--kind"
    assert flags["limit"].arg_type == "int"


def test_op_underscores_become_a_dashed_subcommand() -> None:
    verb = gen._verb_for(_tool("resources.set_scheduling", {"properties": {}}))
    assert (verb.group, verb.sub) == ("resources", "set-scheduling")


def test_discriminated_request_falls_back_to_the_whole_param_json_escape() -> None:
    # A `request` that is a discriminated union (accounting.report, reports.generate,
    # audit.query) has no object body to flatten. Flattening to zero flags would leave the
    # verb with no way to pass its required argument, so it keeps the --request-json escape.
    tool = _tool(
        "accounting.report",
        {
            "properties": {
                "request": {
                    "oneOf": [{"$ref": "#/$defs/Granted"}, {"$ref": "#/$defs/AllProjects"}],
                    "discriminator": {"propertyName": "scope"},
                }
            },
            "required": ["request"],
        },
    )
    verb = gen._verb_for(tool)
    assert verb.unwrap_request is False
    assert verb.flags == ()
    assert verb.json_params == ("request",)


def test_required_scalar_marks_required_flag() -> None:
    tool = _tool(
        "demo.get",
        {"properties": {"id": {"type": "string", "description": "Id."}}, "required": ["id"]},
    )
    (flag,) = gen._verb_for(tool).flags
    assert flag.required is True and flag.arg_type == "str"


def test_boolean_parameter_uses_store_true() -> None:
    flag = gen._flag_for("force", {"type": "boolean", "description": "Force."}, False)
    assert flag is not None and flag.action == "store_true" and flag.arg_type is None


def test_number_parameter_maps_to_float() -> None:
    flag = gen._flag_for("ratio", {"type": "number", "description": "R."}, False)
    assert flag is not None and flag.arg_type == "float"


def test_array_of_string_uses_append() -> None:
    flag = gen._flag_for(
        "packages", {"type": "array", "items": {"type": "string"}, "description": "P."}, False
    )
    assert flag is not None and flag.action == "append" and flag.arg_type == "str"


def test_object_array_defers_to_json() -> None:
    assert gen._flag_for("refs", {"type": "array", "items": {"type": "object"}}, False) is None


def test_scalar_union_defers_to_json() -> None:
    assert gen._flag_for("v", {"anyOf": [{"type": "number"}, {"type": "string"}]}, False) is None


def test_nested_object_parameter_defers_to_json_params() -> None:
    tool = _tool(
        "demo.run",
        {
            "properties": {"profile": {"type": "object", "properties": {"x": {"type": "string"}}}},
            "required": ["profile"],
        },
    )
    verb = gen._verb_for(tool)
    assert verb.flags == () and verb.json_params == ("profile",)


def test_ref_into_defs_resolves_to_its_enum_choices() -> None:
    """A ``$ref``-rendered enum keeps its ``choices`` instead of falling to the JSON escape.

    fastmcp inlines most models, but a reused ``StrEnum`` renders as a bare ``$ref``. Left
    unresolved the parameter looks typeless, so it becomes ``--mode-json`` — which then rejects
    the bare string the enum accepts (#1584, ADR-0469).
    """
    tool = _tool(
        "demo.run",
        {
            "$defs": {"Mode": {"enum": ["passive", "force"], "type": "string"}},
            "properties": {
                "mode": {
                    "anyOf": [{"$ref": "#/$defs/Mode"}, {"type": "null"}],
                    "description": "How to run.",
                }
            },
            "required": ["mode"],
        },
    )
    verb = gen._verb_for(tool)
    assert verb.json_params == ()
    (flag,) = verb.flags
    assert flag.choices == ("passive", "force") and flag.arg_type == "str"
    assert flag.help == "How to run."


def test_ref_into_defs_resolves_a_request_wrapper_body() -> None:
    tool = _tool(
        "demo.list",
        {
            "$defs": {
                "Req": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer"}},
                    "required": [],
                }
            },
            "properties": {"request": {"$ref": "#/$defs/Req"}},
        },
    )
    verb = gen._verb_for(tool)
    assert verb.unwrap_request is True
    assert [f.dest for f in verb.flags] == ["limit"]


def test_dangling_ref_degrades_to_the_json_escape() -> None:
    """An unresolvable reference does not break generation; the param keeps its JSON escape."""
    tool = _tool("demo.run", {"properties": {"mode": {"$ref": "#/$defs/Absent"}}})
    assert gen._verb_for(tool).json_params == ("mode",)


def test_required_tool_with_no_derivable_parameter_raises() -> None:
    """A required-but-flagless verb is a generation error, not a silently uninvokable verb.

    The drift check cannot catch this: it compares an empty generation against an equally empty
    committed descriptor and passes (#1588, ADR-0469).
    """
    tool = _tool("demo.run", {"properties": {}, "required": ["request"]})
    with pytest.raises(ValueError, match="uninvokable"):
        gen._verb_for(tool)


def test_tool_with_no_parameters_at_all_is_not_an_error() -> None:
    assert gen._verb_for(_tool("demo.ping", {"properties": {}})).flags == ()


def test_parameter_deriving_to_a_reserved_flag_raises() -> None:
    reserved = next(iter(RESERVED_CLI_FLAGS)).removeprefix("--").replace("-", "_")
    with pytest.raises(ValueError, match="reserved flag"):
        gen._flag_for(reserved, {"type": "string"}, False)
