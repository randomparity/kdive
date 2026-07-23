"""Structural guard: every verb sits at its canonical derived path, and no path means two tools.

The merged CLI surface (#1448) seeds one verb per registered tool from ``GENERATED_VERBS`` and
lets a curated ``Verb`` override the argparse shape *at its derived path*. These tests are the
proof that the alias table is gone: a curated path that could not be derived from its tool, or a
path that resolved to two different tools, would be an un-checkable alias — exactly what this
issue retired.
"""

from __future__ import annotations

import pytest

from kdive.cli.__main__ import build_parser
from kdive.cli.commands._generated_verbs import GENERATED_VERBS
from kdive.cli.commands.registry import REGISTRY, Verb
from kdive.cli.commands.verb_spec import GeneratedFlag, GeneratedVerb


def _derive_path(tool: str) -> tuple[str, str]:
    """Derive ``(group, subcommand)`` from a tool name, mirroring the generator's rule."""
    namespace, op = tool.split(".", 1)
    return namespace, op.replace("_", "-")


def test_every_curated_verb_sits_at_its_derived_path() -> None:
    for verb in REGISTRY:
        assert (verb.group, verb.sub) == _derive_path(verb.tool), verb.tool


def test_no_path_resolves_to_two_tools() -> None:
    generated_by_path = {(v.group, v.sub): v.tool for v in GENERATED_VERBS}
    for verb in REGISTRY:
        key = (verb.group, verb.sub)
        assert key in generated_by_path, f"curated {key} has no generated verb to override"
        assert generated_by_path[key] == verb.tool, (
            f"path {key} resolves to two tools: {verb.tool} (curated) vs "
            f"{generated_by_path[key]} (generated)"
        )


def test_generated_paths_are_unique() -> None:
    paths = [(v.group, v.sub) for v in GENERATED_VERBS]
    assert len(paths) == len(set(paths)), "a generated path is claimed by two tools"


def _required_argv_for_curated(verb: Verb) -> list[str]:
    argv = [f"{name}-val" for name in verb.positionals]
    for option in verb.required_options:
        argv += [f"--{option.replace('_', '-')}", f"{option}-val"]
    return argv


def _value_for_flag(flag: GeneratedFlag) -> str:
    if flag.choices:
        return flag.choices[0]
    return "1" if flag.arg_type in {"int", "float"} else "x"


def _required_argv_for_generated(verb: GeneratedVerb) -> list[str]:
    argv: list[str] = []
    for flag in verb.flags:
        if not flag.required:
            continue
        if flag.action == "store_true":
            argv.append(flag.name)
        else:
            argv += [flag.name, _value_for_flag(flag)]
    return argv


@pytest.mark.parametrize("generated", GENERATED_VERBS, ids=lambda v: v.tool)
def test_parser_resolves_every_verb_at_its_canonical_path(generated: GeneratedVerb) -> None:
    # Derive each path mechanically and assert the built parser resolves it — no alias table.
    curated = {(v.group, v.sub): v for v in REGISTRY}.get((generated.group, generated.sub))
    tail = (
        _required_argv_for_curated(curated)
        if curated is not None
        else _required_argv_for_generated(generated)
    )
    args = build_parser().parse_args([generated.group, generated.sub, *tail])
    assert (args.command, args.subcommand) == (generated.group, generated.sub)


def test_generated_flag_named_like_a_routing_key_does_not_clobber_routing() -> None:
    # ``control.diagnostic_sysrq`` has a ``--command`` param whose bare dest would overwrite
    # argparse's top-level ``command`` routing key; the namespaced dest keeps routing intact.
    args = build_parser().parse_args(
        ["control", "diagnostic-sysrq", "--system-id", "sys-1", "--command", "s"]
    )
    assert args.command == "control" and args.subcommand == "diagnostic-sysrq"
    assert args.genarg_command == "s"
