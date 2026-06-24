"""The live drgn-script timeout-ceiling setting is registered (ADR-0240)."""

from __future__ import annotations

import kdive.config as config
from kdive.config.core_settings import LIVE_SCRIPT_MAX_TIMEOUT_SECONDS, SETTINGS


def test_live_script_max_timeout_default() -> None:
    assert config.require(LIVE_SCRIPT_MAX_TIMEOUT_SECONDS) == 600


def test_live_script_max_timeout_registered() -> None:
    names = {s.name for s in config.all_settings()}
    assert "KDIVE_LIVE_SCRIPT_MAX_TIMEOUT_SECONDS" in names
    assert LIVE_SCRIPT_MAX_TIMEOUT_SECONDS in SETTINGS


def test_live_script_max_timeout_is_server_scoped() -> None:
    # introspect.script is a synchronous server-side tool (ADR-0033 §1), so the server
    # process is the one that clamps the agent timeout before driving the in-guest bound.
    assert LIVE_SCRIPT_MAX_TIMEOUT_SECONDS.processes == frozenset({"server"})
