"""Stable CLI exit codes derived from kdive ``ToolResponse`` error categories (ADR-0089).

The codes are part of the CLI's contract with scripts and CI, so they are explicit and
fixed: 1 is the generic failure, and each mapped category gets a distinct nonzero code.
"""

from __future__ import annotations

from collections.abc import Mapping

_CODES = {
    "configuration_error": 2,
    "authorization_denied": 3,
    "not_found": 4,
    "conflict": 5,
    "capacity_exhausted": 6,
}


def exit_code_for_category(category: str) -> int:
    """Map an error category to a stable nonzero exit code, defaulting to generic failure."""
    return _CODES.get(category, 1)


def exit_code_for_envelope(envelope: object) -> int:
    """Map a ``ToolResponse`` envelope to its exit code: 0 on success, else the category code.

    A tool denies or fails by *returning* a failure envelope carrying an ``error_category``
    (e.g. ``authorization_denied`` from a role gate), not by raising, so the exit code is
    derived from the envelope here. This is what makes a server-side denial observable as a
    distinct nonzero exit to a script or CI rather than an empty-looking success (ADR-0089).
    """
    if not isinstance(envelope, Mapping):
        return 0
    fields: Mapping[str, object] = {str(k): v for k, v in envelope.items()}
    category = fields.get("error_category")
    if not isinstance(category, str):
        return 0
    return exit_code_for_category(category)
