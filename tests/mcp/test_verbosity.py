"""The compact-responses flag reader (ADR-0314)."""

from __future__ import annotations

import pytest

from kdive.mcp.verbosity import compact_responses_enabled


@pytest.mark.parametrize("value", ["on", "1", "true", "TRUE", "  On  "])
def test_enabled_for_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("KDIVE_COMPACT_RESPONSES", value)
    assert compact_responses_enabled() is True


@pytest.mark.parametrize("value", ["off", "0", "false", "no", ""])
def test_disabled_for_falsy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("KDIVE_COMPACT_RESPONSES", value)
    assert compact_responses_enabled() is False


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_COMPACT_RESPONSES", raising=False)
    assert compact_responses_enabled() is False
