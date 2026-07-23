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
    verb = gen._verb_for(_tool("accounting.report_all_projects", {"properties": {}}))
    assert (verb.group, verb.sub) == ("accounting", "report-all-projects")


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


def test_parameter_deriving_to_a_reserved_flag_raises() -> None:
    reserved = next(iter(RESERVED_CLI_FLAGS)).removeprefix("--").replace("-", "_")
    with pytest.raises(ValueError, match="reserved flag"):
        gen._flag_for(reserved, {"type": "string"}, False)
