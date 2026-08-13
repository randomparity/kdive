"""kdive — Kernel Debug, Inspect, Validate, Explore.

Package import is intentionally inert: the capture bootstrap resolves beneath this package before
its seccomp boundary exists. Version discovery is lazy so that import performs no metadata or
subprocess work (ADR-0558).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    __version__: str


def __getattr__(name: str) -> str:
    """Resolve the public package version only when a caller asks for it."""
    if name != "__version__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from kdive.version import package_version

    return package_version()
