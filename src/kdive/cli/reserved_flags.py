"""Reserved ``kdivectl`` flags and parameter-to-flag derivation (ADR-0421)."""

from __future__ import annotations

from kdive.cli.passthrough import _FLAG_FOR_TIER

# Generated verbs may not shadow global argparse flags or passthrough tier opt-ins (ADR-0422).
RESERVED_CLI_FLAGS: frozenset[str] = frozenset(
    {"--json", "--help", "--yes"} | set(_FLAG_FOR_TIER.values())
)


def derive_cli_flag(param_name: str) -> str:
    """Derive a ``kdivectl`` long option from a tool parameter (ADR-0421)."""
    return "--" + param_name.replace("_", "-")
