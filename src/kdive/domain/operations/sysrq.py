"""Diagnostic magic-SysRq command allowlist (ADR-0285, #925).

The single source of truth for the non-destructive SysRq commands ``control.diagnostic_sysrq``
exposes. Each friendly command name maps to its magic-SysRq trigger character; the provider
resolves the trigger to a keycode. Destructive SysRq keys are absent from the enum by
construction, so they cannot be expressed through the tool — a caller who wants the crash path
uses ``control.force_crash``.
"""

from __future__ import annotations

from enum import StrEnum

from kdive.domain.errors import CategorizedError, ErrorCategory


class SysRqCommand(StrEnum):
    """A non-destructive diagnostic SysRq command, keyed by its friendly agent-facing name."""

    SHOW_TASK_STATES = "show_task_states"
    SHOW_BLOCKED_TASKS = "show_blocked_tasks"
    SHOW_MEMORY = "show_memory"
    SHOW_LOCKS = "show_locks"
    SHOW_REGISTERS = "show_registers"
    SHOW_BACKTRACE_ALL_CPUS = "show_backtrace_all_cpus"
    SHOW_TIMERS = "show_timers"

    @property
    def trigger(self) -> str:
        """The magic-SysRq trigger character (as in ``echo <trigger> > /proc/sysrq-trigger``)."""
        return _SYSRQ_TRIGGERS[self]


_SYSRQ_TRIGGERS: dict[SysRqCommand, str] = {
    SysRqCommand.SHOW_TASK_STATES: "t",
    SysRqCommand.SHOW_BLOCKED_TASKS: "w",
    SysRqCommand.SHOW_MEMORY: "m",
    SysRqCommand.SHOW_LOCKS: "d",
    SysRqCommand.SHOW_REGISTERS: "p",
    SysRqCommand.SHOW_BACKTRACE_ALL_CPUS: "l",
    SysRqCommand.SHOW_TIMERS: "q",
}

# Destructive / non-diagnostic SysRq triggers and their common names. A caller who reaches for
# one gets a redirect to the destructive tool rather than a bare "unknown command" (ADR-0285).
_DESTRUCTIVE_HINTS: frozenset[str] = frozenset(
    {"c", "crash", "b", "reboot", "o", "poweroff", "s", "sync", "u", "e", "i", "f", "k"}
)


def parse_command(value: str) -> SysRqCommand:
    """Resolve a caller-supplied command name to a :class:`SysRqCommand`.

    Args:
        value: The friendly command name from the tool call.

    Returns:
        The matching allowlisted command.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when ``value`` is not an allowlisted command.
            A recognizably destructive request (e.g. ``crash``/``reboot``) carries a
            ``remediation`` naming ``control.force_crash``; any other unknown value lists the
            allowed commands.
    """
    try:
        return SysRqCommand(value)
    except ValueError:
        raise _unknown_command(value) from None


def _unknown_command(value: str) -> CategorizedError:
    details: dict[str, object] = {
        "reason": "unknown_command",
        "allowed": [command.value for command in SysRqCommand],
    }
    if value.strip().lower() in _DESTRUCTIVE_HINTS:
        details["reason"] = "destructive_command"
        details["remediation"] = (
            "destructive SysRq commands are not supported by control.diagnostic_sysrq; "
            "use control.force_crash for the crash path"
        )
    return CategorizedError(
        f"unknown diagnostic SysRq command {value!r}",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details=details,
    )
