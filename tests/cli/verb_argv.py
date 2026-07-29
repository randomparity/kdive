"""Placeholder argv for a CLI verb's required surface, typed off the schema.

Four guards across ``tests/cli/`` need to drive a verb's parser far enough to reach the thing
they actually assert, which means synthesizing a value for every required argument. A bare
``f"{name}-val"`` stopped working once curated parameters gained a real ``type=`` derived from
the generated verb at the same path (ADR-0469, ADR-0474): ``images extend --seconds`` is an
``int`` now, and an enumerated parameter only accepts a member of its schema enum.

The rule is one rule, so it lives in one place: read the :class:`GeneratedFlag` the parser reads,
and emit a value that flag would accept.
"""

from __future__ import annotations

from kdive.cli.commands.registry import Verb, _curated_flags
from kdive.cli.commands.verb_spec import GeneratedFlag, GeneratedVerb


def value_for_flag(flag: GeneratedFlag) -> str:
    """A value ``flag`` accepts: an enum member, a number, or an opaque string."""
    if flag.choices:
        return flag.choices[0]
    return "1" if flag.arg_type in {"int", "float"} else "x"


def value_for_curated(verb: Verb, name: str) -> str:
    """A value ``verb``'s parameter ``name`` accepts, typed off its generated twin.

    A curated parameter with no generated counterpart keeps the opaque ``<name>-val`` placeholder,
    because nothing constrains it.
    """
    flag = _curated_flags(verb).get(name)
    return value_for_flag(flag) if flag is not None else f"{name}-val"


def required_argv_for_curated(verb: Verb, skip: str = "") -> list[str]:
    """Argv satisfying ``verb``'s positionals and required options, except the option ``skip``.

    ``skip`` is a long flag (``--timeout-s``) the caller intends to supply itself, so it is not
    duplicated. It never matches a positional, which takes no flag spelling.
    """
    argv = [value_for_curated(verb, name) for name in verb.positionals]
    for option in verb.required_options:
        flag = f"--{option.replace('_', '-')}"
        if flag != skip:
            argv += [flag, value_for_curated(verb, option)]
    return argv


def required_argv_for_generated(verb: GeneratedVerb, skip: str = "") -> list[str]:
    """Argv satisfying every required flag of ``verb``, except the flag named ``skip``."""
    argv: list[str] = []
    for flag in verb.flags:
        if not flag.required or flag.name == skip:
            continue
        if flag.action in {"store_true", "bool_optional"}:
            argv.append(flag.name)  # both are valueless; bool_optional also accepts --no-<flag>
        else:
            argv += [flag.name, value_for_flag(flag)]
    return argv
