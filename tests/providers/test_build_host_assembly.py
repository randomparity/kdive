"""Provider assembly build-host helper tests."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.assembly import build_hosts


def test_declared_remote_instance_names_degrades_on_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> list[str]:
        raise CategorizedError("bad toml", category=ErrorCategory.CONFIGURATION_ERROR)

    monkeypatch.setattr(build_hosts, "remote_instance_names", _boom)
    assert build_hosts.declared_remote_instance_names() == []


def test_declared_remote_instance_names_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_hosts, "remote_instance_names", lambda: ["a", "b"])
    assert build_hosts.declared_remote_instance_names() == ["a", "b"]
