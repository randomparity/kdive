"""Tests for the diagnostic SysRq command allowlist (ADR-0285, #925)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.sysrq import SysRqCommand, parse_command


def test_each_command_maps_to_its_magic_sysrq_trigger() -> None:
    assert {command.value: command.trigger for command in SysRqCommand} == {
        "show_task_states": "t",
        "show_blocked_tasks": "w",
        "show_memory": "m",
        "show_locks": "d",
        "show_registers": "p",
        "show_backtrace_all_cpus": "l",
        "show_timers": "q",
    }


def test_parse_returns_the_enum_member_for_a_known_command() -> None:
    assert parse_command("show_memory") is SysRqCommand.SHOW_MEMORY


def test_parse_unknown_command_is_configuration_error_listing_allowed() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        parse_command("show_everything")
    error = excinfo.value
    assert error.category is ErrorCategory.CONFIGURATION_ERROR
    assert error.details["reason"] == "unknown_command"
    allowed = error.details["allowed"]
    assert isinstance(allowed, list)
    assert "show_memory" in allowed


@pytest.mark.parametrize("destructive", ["c", "crash", "b", "reboot", "o", "poweroff"])
def test_parse_destructive_command_redirects_to_force_crash(destructive: str) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        parse_command(destructive)
    error = excinfo.value
    assert error.category is ErrorCategory.CONFIGURATION_ERROR
    assert error.details["reason"] == "destructive_command"
    assert "control.force_crash" in str(error.details["remediation"])
