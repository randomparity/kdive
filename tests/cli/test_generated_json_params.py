"""The ``--<param>-json`` escape for non-scalar generated-verb parameters (#1449).

Non-scalar tool parameters (nested objects, object arrays) cannot derive to a typed scalar
flag, so the generator records them in ``GeneratedVerb.json_params`` (#1447) and defers the CLI
surface here: each marked param gets a ``--<param>-json`` flag whose value is validated to a
JSON container (object or array) at parse time, so a malformed or scalar value fails as a clean
usage error (exit 2) before the verb dispatches. The flag-value-to-payload assembly is #1450;
here the value only lands on the namespace under the ``genarg_<param>_json`` dest.
"""

from __future__ import annotations

import pytest

from kdive.cli.__main__ import build_parser
from kdive.cli.commands._generated_verbs import GENERATED_VERBS
from kdive.cli.commands.registry import GENERATED_ARG_PREFIX, REGISTRY
from kdive.cli.commands.verb_spec import GeneratedFlag, GeneratedVerb

# Every generated verb that carries at least one non-scalar (``json_params``) parameter. A
# curated ``Verb`` overrides the argparse shape at its path, so those paths do not take the
# generated json-flag surface and are excluded.
_CURATED_PATHS = {(v.group, v.sub) for v in REGISTRY}
_JSON_PARAM_VERBS = [
    v for v in GENERATED_VERBS if v.json_params and (v.group, v.sub) not in _CURATED_PATHS
]

# The specific tools the issue names as carrying nested-object / object-array params.
_TARGET_TOOLS = {
    "systems.provision",
    "systems.define",
    "systems.reprovision",
    "runs.create",
    "artifacts.create_run_upload",
    "artifacts.create_system_upload",
    "investigations.link",
    "investigations.unlink",
}


def _value_for_flag(flag: GeneratedFlag) -> str:
    if flag.choices:
        return flag.choices[0]
    return "1" if flag.arg_type in {"int", "float"} else "x"


def _required_argv(verb: GeneratedVerb) -> list[str]:
    """Minimal argv (past ``group sub``) that satisfies the verb's required scalar flags."""
    argv: list[str] = []
    for flag in verb.flags:
        if not flag.required:
            continue
        if flag.action == "store_true":
            argv.append(flag.name)
        else:
            argv += [flag.name, _value_for_flag(flag)]
    return argv


def _json_flag(param: str) -> str:
    return f"--{param.replace('_', '-')}-json"


def _json_dest(param: str) -> str:
    return f"{GENERATED_ARG_PREFIX}{param}_json"


def test_target_tools_are_present_in_the_generated_surface() -> None:
    # Guard the fixtures: if a rename drops one of the named tools, fail loudly rather than
    # silently testing fewer verbs.
    present = {v.tool for v in _JSON_PARAM_VERBS}
    assert present >= _TARGET_TOOLS, _TARGET_TOOLS - present


@pytest.mark.parametrize("verb", _JSON_PARAM_VERBS, ids=lambda v: v.tool)
def test_every_json_param_gets_a_json_flag(verb: GeneratedVerb) -> None:
    # Each ``json_params`` name is reachable as ``--<param>-json`` and lands under its
    # ``genarg_<param>_json`` dest holding the raw string.
    argv = [verb.group, verb.sub, *_required_argv(verb)]
    for param in verb.json_params:
        argv += [_json_flag(param), "{}"]
    args = build_parser().parse_args(argv)
    for param in verb.json_params:
        assert getattr(args, _json_dest(param)) == "{}"


@pytest.mark.parametrize("verb", _JSON_PARAM_VERBS, ids=lambda v: v.tool)
def test_json_flag_defaults_to_none_when_absent(verb: GeneratedVerb) -> None:
    args = build_parser().parse_args([verb.group, verb.sub, *_required_argv(verb)])
    for param in verb.json_params:
        assert getattr(args, _json_dest(param)) is None


def test_profile_json_accepts_a_valid_object() -> None:
    args = build_parser().parse_args(
        ["systems", "provision", "--allocation-id", "al-1", "--profile-json", '{"arch": "x86_64"}']
    )
    assert args.genarg_profile_json == '{"arch": "x86_64"}'


def test_artifacts_json_accepts_a_valid_array() -> None:
    # ``artifacts`` is a ``Sequence[ArtifactDeclaration]`` (JSON array), so object-only
    # validation would falsely reject its correct payload.
    args = build_parser().parse_args(
        [
            "artifacts",
            "create-run-upload",
            "--run-id",
            "run-1",
            "--artifacts-json",
            '[{"name": "vmlinuz"}]',
        ]
    )
    assert args.genarg_artifacts_json == '[{"name": "vmlinuz"}]'


def test_malformed_json_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(
            ["systems", "provision", "--allocation-id", "al-1", "--profile-json", "{not json"]
        )
    assert excinfo.value.code == 2


def test_scalar_json_is_a_usage_error() -> None:
    # A bare scalar is valid JSON but not a structured param value; reject before dispatch.
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(
            ["investigations", "link", "--investigation-id", "inv-1", "--ref-json", "5"]
        )
    assert excinfo.value.code == 2


def test_empty_string_json_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(
            ["systems", "reprovision", "--system-id", "sys-1", "--profile-json", ""]
        )
    assert excinfo.value.code == 2
