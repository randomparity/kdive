"""Build a :class:`KernelConfig` for a test whose y/m split is not the thing under test.

``KernelConfig.builtin`` deliberately does not default (ADR-0544 §2, #1860): a default of empty
would reinterpret every pre-existing positional fixture as a wholly modular kernel and fail an
``UNLESS_INITRD`` clause for a reason the test never intended. This helper names the common
intent - "a kernel with all of these built in" - so a fixture that does not care about the split
says so, and a fixture that does care still writes both sets out by hand.
"""

from __future__ import annotations

from collections.abc import Iterable

from kdive.kernel_config.parse import KernelConfig


def all_builtin(symbols: Iterable[str]) -> KernelConfig:
    """A config enabling ``symbols``, every one of them ``=y``."""
    both = frozenset(symbols)
    return KernelConfig(both, both)
