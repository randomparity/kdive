"""Structural guard: descriptors own every CLI path and handler overrides remain tool-keyed."""

from __future__ import annotations

import pytest

from kdive.cli.__main__ import build_parser
from kdive.cli.commands._generated_verbs import GENERATED_VERBS
from kdive.cli.commands.registry import HANDLER_OVERRIDES
from kdive.cli.commands.verb_spec import GeneratedVerb
from tests.cli.verb_argv import required_argv_for_generated


def _derive_path(tool: str) -> tuple[str, str]:
    """Derive ``(group, subcommand)`` from a tool name, mirroring the generator's rule."""
    namespace, op = tool.split(".", 1)
    return namespace, op.replace("_", "-")


def test_handler_overrides_only_live_generated_tools() -> None:
    generated_tools = {verb.tool for verb in GENERATED_VERBS}
    assert set(HANDLER_OVERRIDES) <= generated_tools


def test_generated_paths_are_unique() -> None:
    paths = [(v.group, v.sub) for v in GENERATED_VERBS]
    assert len(paths) == len(set(paths)), "a generated path is claimed by two tools"


@pytest.mark.parametrize("generated", GENERATED_VERBS, ids=lambda v: v.tool)
def test_parser_resolves_every_verb_at_its_canonical_path(generated: GeneratedVerb) -> None:
    # Derive each path mechanically and assert the built parser resolves it — no alias table.
    # The placeholder values are typed off the same flags the parser reads (ADR-0469, ADR-0474):
    # a bare "seconds-val" stopped parsing once `images extend --seconds` became a real int.
    tail = required_argv_for_generated(generated)
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


def test_required_boolean_flag_parses_both_states() -> None:
    # ``ops.set_queue_paused`` is the only required boolean; ``store_true`` would leave the
    # false state — resuming the queue — unreachable from the CLI, so it uses --flag/--no-flag.
    parser = build_parser()
    paused = parser.parse_args(["ops", "set-queue-paused", "--paused"])
    resumed = parser.parse_args(["ops", "set-queue-paused", "--no-paused"])
    assert paused.genarg_paused is True
    assert resumed.genarg_paused is False


def test_required_boolean_flag_must_be_given() -> None:
    # Required means required: an omitted target state is a parse error, not a silent default.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["ops", "set-queue-paused"])
