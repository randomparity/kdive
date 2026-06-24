"""The introspection-mode capability vocabulary (ADR-0208/0240)."""

from __future__ import annotations

from kdive.providers.ports import IntrospectionMode  # noqa: F401 - re-export smoke
from kdive.providers.ports.lifecycle import INTROSPECTION_MODES


def test_live_script_is_a_known_introspection_mode() -> None:
    assert "live-script" in INTROSPECTION_MODES
