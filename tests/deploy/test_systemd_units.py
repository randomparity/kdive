"""Structural checks on the shipped systemd units.

`systemd-analyze verify` needs systemd and is environment-gated; these unit-file
assertions run everywhere and lock in the backend-retry contract (ADR-0114 §4).
"""

from __future__ import annotations

from pathlib import Path

import pytest

SYSTEM = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "system"
SERVICES = ("kdive-server", "kdive-worker", "kdive-reconciler")


@pytest.mark.parametrize("name", SERVICES)
def test_system_unit_has_retry_contract(name: str) -> None:
    text = (SYSTEM / f"{name}.service").read_text()
    assert "Restart=on-failure" in text
    assert "RestartSec=" in text
    assert "After=network-online.target" in text
    assert "EnvironmentFile=" in text
    assert "User=kdive" in text


@pytest.mark.parametrize("name", SERVICES)
def test_system_unit_exec_matches_process(name: str) -> None:
    text = (SYSTEM / f"{name}.service").read_text()
    process = name.removeprefix("kdive-")
    assert f"-m kdive {process}" in text
