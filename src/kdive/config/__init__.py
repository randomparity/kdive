"""Central typed configuration registry for the ``KDIVE_*`` contract (ADR-0087).

This package is the single declared source of truth for every ``KDIVE_*`` variable.
Point-of-use code reads through :func:`get` instead of ``os.environ``; startup
:func:`validate` and the generated reference both derive from the same declarations.
"""

from __future__ import annotations

from kdive.config.registry import Registry, Setting

__all__ = ["Registry", "Setting"]
