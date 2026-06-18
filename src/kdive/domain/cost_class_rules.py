"""The single name/coeff validation rule for a cost class (ADR-0115 §1).

Both the inventory model (``[[cost_class]]`` in ``systems.toml``) and the imperative
``ops.set_cost_class_coeff`` tool validate a cost class the same way. To keep the two
surfaces from diverging — and without ``inventory/`` importing ``mcp/tools/ops`` (a
core→tool layering inversion) — the rule lives here, neutral. It raises a bare
:class:`ValueError`; each caller maps it to its own error type (``InventoryError`` at file
load, ``CONFIGURATION_ERROR`` for the tool).
"""

from __future__ import annotations

import re
from decimal import Decimal, DecimalException, InvalidOperation

_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


def validate_cost_class_name(name: str) -> str:
    """Return ``name`` if it is a non-blank token; raise ``ValueError`` otherwise (fail closed).

    A blank class would seed an unreachable junk row no host can carry. The charset is
    restricted to ``[A-Za-z0-9._-]`` so a name can never carry a TOML-significant character
    (``"``, newline, ``[``/``]``/``=``/``#``); ``ops.export_cost_classes`` interpolates the
    name into a ``[[cost_class]]`` fragment, and an unescaped quote/newline would corrupt or
    silently mis-parse the exported TOML on the export→commit→reconcile loop.
    """
    if not name.strip():
        raise ValueError(f"cost_class name {name!r} must be non-blank")
    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"cost_class name {name!r} must contain only letters, digits, '.', '_', or '-'"
        )
    return name


def parse_positive_coeff(value: object) -> Decimal:
    """Parse ``value`` into a finite, positive coefficient (fail closed).

    Uses ``Decimal(str(value))`` so a TOML float does not introduce binary-float drift.
    A coefficient is a price multiplier; ``0`` or negative would price work as free or as a
    budget credit, so both are rejected, as is a non-finite (``nan``/``inf``) value.
    """
    try:
        parsed = Decimal(str(value))
    except InvalidOperation, DecimalException, ValueError, TypeError:
        raise ValueError(f"coeff {value!r} is not a number") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"coeff {value!r} must be a finite number > 0")
    return parsed
