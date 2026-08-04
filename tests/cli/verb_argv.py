"""Placeholder argv for a generated CLI verb's required surface, typed off the schema.

Four guards across ``tests/cli/`` need to drive a verb's parser far enough to reach the thing
they actually assert, which means synthesizing a value for every required argument. A bare
``f"{name}-val"`` stopped working once generated parameters gained a real ``type=`` (ADR-0469,
ADR-0474): ``images extend --seconds`` is an ``int`` now, and an enumerated parameter only
accepts a member of its schema enum.

The rule is one rule, so it lives in one place: read the :class:`GeneratedFlag` the parser reads,
and emit a value that flag would accept.
"""

from __future__ import annotations

from kdive.cli.commands.verb_spec import GeneratedFlag, GeneratedVerb


def value_for_flag(flag: GeneratedFlag) -> str:
    """A value ``flag`` accepts: an enum member, a number, or an opaque string."""
    if flag.choices:
        return flag.choices[0]
    return "1" if flag.arg_type in {"int", "float"} else "x"


def required_argv_for_generated(verb: GeneratedVerb, skip: str = "") -> list[str]:
    """Argv satisfying every required field of ``verb``, except the flag named ``skip``."""
    by_dest = {flag.dest: flag for flag in verb.flags}
    argv = [value_for_flag(by_dest[name]) for name in verb.positionals]
    for flag in verb.flags:
        if flag.dest in verb.positionals or not flag.required or flag.name == skip:
            continue
        if flag.action in {"store_true", "bool_optional"}:
            argv.append(flag.name)  # both are valueless; bool_optional also accepts --no-<flag>
        else:
            argv += [flag.name, value_for_flag(flag)]
    return argv
