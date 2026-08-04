"""A numeric CLI argument is coerced at the parser seam, and a float must be finite (ADR-0474).

Every option's coercion comes from its generated descriptor, including fields selected by a
presentation overlay for a specialised handler (ADR-0469). These tests drive the real
``build_parser`` rather than a handler, because the defect they pin (#1619) is that a malformed
value used to survive parsing and die later in a handler's bare ``int()``.

``test_verb_schema_guard.py`` cannot reach this class: its ``_sample_value`` synthesizes only
values the schema would accept, so it never drives a malformed one.
"""

from __future__ import annotations

import argparse

import pytest
from _pytest.mark import ParameterSet

from kdive.cli.__main__ import build_parser, main
from kdive.cli.commands._generated_verbs import GENERATED_VERBS
from kdive.cli.commands.registry import _ARG_TYPES, GENERATED_ARG_PREFIX
from kdive.cli.commands.verb_spec import GeneratedFlag, GeneratedVerb
from tests.cli.verb_argv import required_argv_for_generated

#: Numeric fields selected by a presentation overlay, with the argv tail that reaches each one
#: and the Python type it must land on the namespace as. This explicit matrix makes a silent
#: widening or narrowing of that operator-facing set visible.
_NUMERIC = [
    pytest.param(["jobs", "wait", "job-1"], "--timeout-s", "timeout_s", float, id="jobs-wait"),
    pytest.param(
        ["allocations", "wait", "alloc-1"],
        "--timeout-s",
        "timeout_s",
        float,
        id="allocations-wait",
    ),
    pytest.param(
        [
            "images",
            "upload",
            "--project",
            "p",
            "--name",
            "n",
            "--arch",
            "x86_64",
            "--quarantine-key",
            "k",
        ],
        "--lifetime-seconds",
        "lifetime_seconds",
        int,
        id="images-upload",
    ),
    pytest.param(
        ["images", "extend", "img-1", "--reason", "r"],
        "--seconds",
        "seconds",
        int,
        id="images-extend",
    ),
]

#: Values no numeric option may accept. ``inf``/``nan`` are here because ``float()`` takes them
#: while JSON does not: pydantic serializes both to ``null``, so the tool never sees a number to
#: reject and silently applies its own default (ADR-0474 decision 2). ``1e400`` is the same class
#: spelled without an obvious ``inf``: it overflows to one.
_MALFORMED = ["abc", "", "1.2.3", "inf", "-inf", "nan", "NaN", "infinity", "1e", "0x10", "1e400"]


@pytest.mark.parametrize(("head", "flag", "dest", "expected"), _NUMERIC)
@pytest.mark.parametrize("value", _MALFORMED)
def test_numeric_option_refuses_a_malformed_value(
    head: list[str], flag: str, dest: str, expected: type, value: str, capsys
) -> None:
    """A non-numeric value is an argparse usage error (exit 2), never a handler ``ValueError``.

    Before #1619 these reached the handler as ``str`` and died in a bare ``int()`` with a
    traceback on exit ``1`` — the one malformed argument on the whole CLI that did not produce
    the usage error every other one does.

    The value is attached with ``=`` so every case actually reaches the type callable. Argparse's
    negative-number heuristic matches only a leading digit or ``.``, so a bare ``--flag -inf``
    fails earlier with "expected one argument" and would pass no matter what the callable did.
    """
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args([*head, f"{flag}={value}"])
    assert excinfo.value.code == 2
    assert f"argument {flag}:" in capsys.readouterr().err


@pytest.mark.parametrize(("head", "flag", "dest", "expected"), _NUMERIC)
def test_numeric_option_lands_as_a_number(
    head: list[str], flag: str, dest: str, expected: type
) -> None:
    """A well-formed value arrives on the namespace already coerced, not as the raw ``str``."""
    args = build_parser().parse_args([*head, flag, "12"])
    assert getattr(args, f"{GENERATED_ARG_PREFIX}{dest}") == 12
    assert isinstance(getattr(args, f"{GENERATED_ARG_PREFIX}{dest}"), expected)


@pytest.mark.parametrize(("head", "flag", "dest", "expected"), _NUMERIC)
def test_numeric_option_omitted_stays_none(
    head: list[str], flag: str, dest: str, expected: type
) -> None:
    """Coercion never fires for an absent option, so an omitted value stays ``None``.

    The two ``wait`` verbs read ``None`` as "send no ``timeout_s``", leaving the tool's own
    30-second default authoritative (ADR-0470 decision 2). A ``type=`` that defaulted the value
    would silently turn a bare ``wait`` into something else.
    """
    if flag == "--seconds":
        pytest.skip(f"{flag} is a required option and cannot be omitted")
    args = build_parser().parse_args(head)
    assert getattr(args, f"{GENERATED_ARG_PREFIX}{dest}") is None


@pytest.mark.parametrize("value", ["3.5", "0", "-1.5"])
def test_float_option_keeps_a_fractional_value(value: str) -> None:
    """``--timeout-s`` is a float, not an int: a fractional timeout must survive the seam.

    ``-1.5`` pins the *sign* as well as the fraction — a coercion that quietly took the magnitude
    would satisfy every refusal assertion in this module while turning a rejected negative into
    an accepted wait.
    """
    args = build_parser().parse_args(["jobs", "wait", "job-1", f"--timeout-s={value}"])
    assert args.genarg_timeout_s == float(value)
    assert isinstance(args.genarg_timeout_s, float)


@pytest.mark.parametrize("value", ["1.5", "0.5", "2e3"])
def test_int_option_refuses_a_non_integer(value: str) -> None:
    """``--seconds`` is an int; a fractional value is a usage error, not a silent truncation."""
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(
            ["images", "extend", "img-1", "--reason", "r", "--seconds", value]
        )
    assert excinfo.value.code == 2


def test_int_option_accepts_a_negative_value() -> None:
    """A negative integer parses: a range bound is not something the parser can answer.

    ``GeneratedFlag`` carries no schema ``minimum`` (ADR-0474 decision 3), so the seam types the
    value and stops there. Pinning this keeps the boundary honest — a future ``minimum`` would
    have to be a deliberate change here, not an accident.
    """
    args = build_parser().parse_args(
        ["images", "extend", "img-1", "--reason", "r", "--seconds", "-1"]
    )
    assert args.genarg_seconds == -1


def _required_tail(verb: GeneratedVerb, skip: str) -> list[str]:
    """Argv satisfying every required argument of ``verb``'s *live* parser, except ``skip``.

    Without this the parser exits 2 on the missing required argument rather than on the value
    under test, which would make every assertion below pass no matter what ``_ARG_TYPES`` holds —
    the exact vacuity this helper exists to prevent.

    The generated descriptor for ``jobs wait`` records a positional id and no ``--job-id``.
    Required argv therefore has to follow the descriptor's presentation metadata, not assume
    every schema property is exposed as an option.
    """
    return required_argv_for_generated(verb, skip)


def _generated_float_flags() -> list[ParameterSet]:
    """Every generated flag the schema types as a JSON ``number``, as argv-ready params."""
    return [
        pytest.param(verb, flag, id=f"{verb.group}-{verb.sub}{flag.name}")
        for verb in GENERATED_VERBS
        for flag in verb.flags
        if flag.arg_type == "float"
    ]


@pytest.mark.parametrize(("verb", "flag"), _generated_float_flags())
@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "Infinity", "1e400"])
def test_generated_float_flag_refuses_a_non_finite_value(
    verb: GeneratedVerb, flag: GeneratedFlag, value: str, capsys
) -> None:
    """The finite check is repo-wide, not scoped to the four overlaid parameters (decision 2).

    ``_ARG_TYPES`` is the one map every descriptor flag uses, so every generated float flag is
    covered by construction. All six are timeouts or deadlines in seconds, where an infinite
    or undefined value has no meaning the wire format could carry.

    The whole required surface is supplied and the *message* is asserted, not just the exit code:
    an argv missing a required argument exits 2 on its own, which would make this pass with the
    finite check reverted to the builtin ``float``. ``-inf`` is spelled with ``=`` because
    argparse's negative-number heuristic only recognizes a leading digit or ``.``, so a bare
    ``--flag -inf`` reads as an option and fails with "expected one argument" before any type
    callable runs.
    """
    argv = [verb.group, verb.sub, *_required_tail(verb, flag.name), f"{flag.name}={value}"]
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(argv)
    assert excinfo.value.code == 2
    # Matched as one string: argparse prints a usage line naming every declared option, so a bare
    # `flag.name in err` would be satisfied even when a *different* argument was the one to fail.
    err = capsys.readouterr().err
    assert f"argument {flag.name}: must be a finite number" in err, err


@pytest.mark.parametrize(("verb", "flag"), _generated_float_flags())
def test_generated_float_flag_accepts_a_finite_value(
    verb: GeneratedVerb, flag: GeneratedFlag
) -> None:
    """The counterpart to the refusal above: a finite value still parses, and lands as a float.

    Without this, tightening ``_ARG_TYPES["float"]`` all the way to "reject everything" would
    satisfy every refusal assertion in this module.
    """
    argv = [verb.group, verb.sub, *_required_tail(verb, flag.name), f"{flag.name}=1.5"]
    args = build_parser().parse_args(argv)
    value = getattr(args, f"{GENERATED_ARG_PREFIX}{flag.dest}", None)
    assert value == 1.5
    assert isinstance(value, float)


def test_every_generated_float_flag_is_covered() -> None:
    """The parametrization above is not vacuous: the schema really does declare float flags.

    A generator change that stopped emitting ``arg_type="float"`` would otherwise turn the
    repo-wide claim into zero test cases that all pass. Pinned to the exact count, not a floor,
    because ADR-0474 decision 2 *enumerates* the blast radius: a seventh float flag would widen
    what the finite check refuses for a new operator-facing argument, and that belongs in the
    record rather than sliding under a ``>=``.
    """
    assert len(_generated_float_flags()) == 6, "ADR-0474 decision 2 enumerates exactly 6"


def test_accounting_convenience_parameters_stay_strings() -> None:
    """Accounting's union-derived presentation fields remain strings (ADR-0469).

    Their tool schemas wrap the values in discriminated unions. The generator recursively finds
    the selected branch properties and emits string flags for the operator-facing overlay. Each
    must accept an arbitrary string rather than being coerced or, by the argparse trap, rejected
    outright.
    """
    args = build_parser().parse_args(
        ["accounting", "report", "--scope", "anything", "--since", "2026-01-01"]
    )
    assert args.genarg_scope == "anything"
    assert isinstance(args.genarg_since, str)


def test_store_true_flags_still_build_and_parse() -> None:
    """``store_true`` takes neither ``type`` nor ``choices``; passing either is a build-time crash.

    ``_StoreTrueAction.__init__`` accepts no such keyword, so a shared ``**kwargs`` helper across
    the four ``_verb_parser`` buckets raises ``TypeError`` while the parser is being constructed —
    which would break every command, not only these. Parsing one flag from each bucket proves the
    tree built (decision 1).
    """
    prune = build_parser().parse_args(["images", "prune-expired", "--expired", "--reason", "r"])
    assert prune.expired
    teardown = build_parser().parse_args(
        ["ops", "force-teardown", "sys-1", "--reason", "r", "--force"]
    )
    assert teardown.force


def test_descriptor_types_are_not_restated() -> None:
    """Each parser action uses exactly the descriptor's type mapping."""
    parser = build_parser()
    actions = {
        (group, sub): {a.dest: a for a in choice._actions}  # noqa: SLF001 - argparse exposes no public read
        for group, group_parser in _subparser_choices(parser).items()
        for sub, choice in _subparser_choices(group_parser).items()
    }
    numeric = 0
    for verb in GENERATED_VERBS:
        by_dest = actions.get((verb.group, verb.sub), {})
        for flag in verb.flags:
            action = by_dest.get(f"{GENERATED_ARG_PREFIX}{flag.dest}")
            if action is None or flag.arg_type is None or flag.action == "append":
                continue
            assert action.type is _ARG_TYPES[flag.arg_type], (
                f"{verb.group} {verb.sub} {flag.name} is {action.type!r}, "
                f"not the derived {_ARG_TYPES[flag.arg_type]!r}"
            )
            numeric += flag.arg_type != "str"
    assert numeric >= 4, f"the numeric descriptor surface disappeared ({numeric})"


def _subparser_choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """The sub-parsers ``parser`` dispatches to, keyed by the name that selects them."""
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public accessor
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return {
                name: choice
                for name, choice in action.choices.items()
                if isinstance(choice, argparse.ArgumentParser)
            }
    return {}


@pytest.mark.parametrize(
    "argv",
    [
        ["images", "extend", "img-1", "--seconds", "abc", "--reason", "r"],
        [
            "images",
            "upload",
            "--project",
            "p",
            "--name",
            "n",
            "--arch",
            "x86_64",
            "--quarantine-key",
            "k",
            "--lifetime-seconds",
            "abc",
        ],
        ["jobs", "wait", "job-1", "--timeout-s", "inf"],
    ],
)
def test_main_exits_two_without_reaching_the_transport(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue's own acceptance criteria, driven through the real entrypoint.

    ``dispatch.run`` catches only ``ToolError`` and ``main`` catches nothing, so before #1619 a
    ``ValueError`` from a handler escaped as a traceback on exit ``1``. Failing in the parser
    also means these two *mutating* verbs now fail before any tool call rather than during
    payload assembly — asserted by making any transport construction an error.
    """

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the transport must not be reached for a malformed argument")

    monkeypatch.setattr("kdive.cli.dispatch.Session", _explode)
    monkeypatch.setattr("kdive.cli.dispatch.run", _explode)
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2
