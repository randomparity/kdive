"""Validation contract for caller-controlled kernel command-line extras."""

from typing import Final

MAX_CMDLINE_EXTRA_LENGTH: Final = 4096


def cmdline_extra_error(cmdline: str | None) -> str | None:
    """Return the public reason code when caller-controlled kernel args are unsafe."""
    if cmdline is None:
        return None
    stripped = cmdline.strip()
    if not stripped:
        return "cmdline_blank"
    if len(stripped) > MAX_CMDLINE_EXTRA_LENGTH:
        return "cmdline_too_long"
    if not stripped.isprintable():
        return "cmdline_not_printable"
    return None
