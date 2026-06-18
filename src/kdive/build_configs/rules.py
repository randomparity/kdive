"""Neutral build-config validation rules shared by the tool and the inventory model (ADR-0122).

``buildconfig.set`` (``mcp/tools/catalog/build_configs.py``) and the ``systems.toml``
``[[build_config]]`` inventory model validate a fragment the same way. To keep the two surfaces
from diverging -- and without ``inventory/`` importing ``mcp/`` (a core->tool layering inversion)
-- the rules live here, neutral. Each raises a bare :class:`ValueError`; callers map it
(``InventoryError`` at file load, ``CONFIGURATION_ERROR`` for the tool). The byte cap is a pure
predicate taking the cap as an argument, so this module imports no config singleton (mirrors
``domain/accounting/cost_class_rules.py``).
"""

from __future__ import annotations

import re

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def validate_build_config_name(name: str) -> str:
    """Return ``name`` if it matches ``^[a-z0-9][a-z0-9_-]{0,63}$``; raise ``ValueError`` otherwise.

    The name folds into the reserved object key, so a strict charset is enforced before it
    reaches the key builder (which blocks only ``/`` and control chars, not ``..``/whitespace/case).
    """
    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError(f"build-config name {name!r} must match ^[a-z0-9][a-z0-9_-]{{0,63}}$")
    return name


def validate_build_config_content(content: str) -> str:
    """Return ``content`` if it is non-empty; raise ``ValueError`` otherwise (fail closed).

    The byte cap is NOT checked here -- it is config-dependent and enforced by the caller that
    has config access (``reconcile-systems --check`` and the reconcile pass), via
    :func:`exceeds_build_config_cap`.
    """
    if not content:
        raise ValueError("build-config content must be non-empty")
    return content


def exceeds_build_config_cap(data: bytes, cap: int) -> bool:
    return len(data) > cap
