"""Reserved ``kdivectl`` flags and the ADR-0421 parameter-to-flag derivation rule.

A single source for the flag names a generated verb may not reuse (epic #1442 R2,
ADR-0421 decision 2 / ADR-0422) and the canonical rule that turns a tool parameter
name into the CLI flag it would generate. The collision guard
(``tests/mcp/core/test_cli_flag_collision.py``) imports both to assert that no
registered tool's parameter derives to a reserved flag; the future schema-driven verb
generator (#1447) reuses them so the guard and the generator share one definition.
"""

from __future__ import annotations

from kdive.cli.passthrough import _FLAG_FOR_TIER

# The reserved generated-verb flag set (ADR-0422). Source of truth per flag:
#   --json  / --yes                -> kdive.cli.__main__.build_parser (the top-level
#                                     machine-output flag and the destructive `tool call`
#                                     confirm, ADR-0421 decision 4).
#   --help                         -> argparse's built-in help flag, present on every parser.
#   --allow-mutating / --allow-destructive
#                                  -> the `tool call` passthrough tier opt-ins
#                                     (kdive.cli.passthrough, ADR-0107). They live only on the
#                                     passthrough, not on generated verbs, but are reserved
#                                     defensively so a future generated verb can never shadow
#                                     them. Sourced from _FLAG_FOR_TIER so this set cannot drift
#                                     from the passthrough's own definition.
RESERVED_CLI_FLAGS: frozenset[str] = frozenset(
    {"--json", "--help", "--yes"} | set(_FLAG_FOR_TIER.values())
)


def derive_cli_flag(param_name: str) -> str:
    """Return the ``kdivectl`` flag a tool parameter derives to (ADR-0421 decision 2).

    Underscores become dashes and the name is prefixed with ``--``, mirroring the verb rule
    that turns ``<op_with_underscores>`` into a dashed subcommand. For example
    ``idempotency_key`` -> ``--idempotency-key`` and ``force`` -> ``--force``.

    Args:
        param_name: A tool input-schema parameter name.

    Returns:
        The derived long-form CLI flag.
    """
    return "--" + param_name.replace("_", "-")
