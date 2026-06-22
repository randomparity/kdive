"""The shipped minimal local-libvirt example inventory validates and arms kdump (#690).

The walkthrough points a novice at ``docs/operating/providers/examples/systems-local-libvirt.toml``
as their first ``systems.toml``. Two invariants keep that promise honest:

* it passes ``reconcile-systems --check`` (the walkthrough's acceptance criterion), and
* its inline ``kdump`` fragment carries every symbol the packaged seed does — because declaring
  ``[[build_config]] name = "kdump"`` makes this file authoritative and a dropped arming symbol
  silently disarms kdump (no vmcore; #679, #688).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from kdive.build_configs.seed import KDUMP_FRAGMENT_PATH
from kdive.inventory.reconcile_cli import validate_systems

_EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "operating"
    / "providers"
    / "examples"
    / "systems-local-libvirt.toml"
)


def _config_symbols(text: str) -> set[str]:
    """The set of ``CONFIG_*`` lines in a kernel-config fragment, whitespace-normalised."""
    return {line.strip() for line in text.splitlines() if line.strip().startswith("CONFIG_")}


def test_minimal_example_validates() -> None:
    assert validate_systems(_EXAMPLE) == 0


def test_minimal_example_kdump_fragment_carries_full_arming_set() -> None:
    doc = tomllib.loads(_EXAMPLE.read_text(encoding="utf-8"))
    kdump = next(block for block in doc["build_config"] if block["name"] == "kdump")
    packaged = _config_symbols(KDUMP_FRAGMENT_PATH.read_text(encoding="utf-8"))
    missing = packaged - _config_symbols(kdump["content"])
    assert not missing, f"example kdump fragment dropped packaged arming symbols: {sorted(missing)}"
