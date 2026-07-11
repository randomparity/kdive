"""Parse a Linux kernel ``.config`` into the set of enabled symbols (ADR-0318).

Pure and tolerant: a malformed / truncated / non-config upload yields a degenerate (empty)
result rather than raising, so the gate can fail open on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# CONFIG_<SYM>=y or =m -> enabled. Anything else (=n, "string", 123, "is not set") -> not.
_ENABLED = re.compile(r"^CONFIG_([A-Z0-9_]+)=(y|m)\s*$")


@dataclass(frozen=True, slots=True)
class KernelConfig:
    """The set of enabled kernel symbols (bare, no ``CONFIG_`` prefix)."""

    enabled: frozenset[str]

    def is_enabled(self, symbol: str) -> bool:
        return symbol in self.enabled

    @property
    def is_degenerate(self) -> bool:
        return not self.enabled


def parse_kernel_config(data: bytes) -> KernelConfig:
    """Parse ``.config`` bytes into a :class:`KernelConfig` of enabled symbols."""
    text = data.decode("utf-8", "replace")
    enabled = {m.group(1) for line in text.splitlines() if (m := _ENABLED.match(line.strip()))}
    return KernelConfig(enabled=frozenset(enabled))
