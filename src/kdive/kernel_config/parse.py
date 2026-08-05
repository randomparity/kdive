"""Parse a Linux kernel ``.config`` into the enabled symbols and the built-in subset (ADR-0318).

The built-in subset, and why ``enabled`` goes on counting ``=m``, are ADR-0544 §2.

Pure and tolerant: a malformed / truncated / non-config upload yields a degenerate (empty)
result rather than raising, so the gate can fail open on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# CONFIG_<SYM>=y or =m -> enabled. Anything else (=n, "string", 123, "is not set") -> not.
# The captured value is what separates the two sets below; the pattern itself deliberately still
# matches both (#1860), because =m is the answer the agent asked for on KASAN, ftrace, kcov and
# the BPF symbols, and narrowing it here would trade one wrong answer for a dozen.
_ENABLED = re.compile(r"^CONFIG_([A-Z0-9_]+)=(y|m)\s*$")


@dataclass(frozen=True, slots=True)
class KernelConfig:
    """Enabled kernel symbols (bare, no ``CONFIG_`` prefix) and the ``=y`` subset of them.

    ``enabled`` means ``=y`` **or** ``=m``, unchanged. ``builtin`` is the ``=y`` half, which the
    boot-critical clauses need because a direct-kernel boot with no initramfs has nothing to load
    a module from before root is mounted. It does not default: a default of empty would read
    every pre-existing positional construction as a wholly modular kernel.
    """

    enabled: frozenset[str]
    builtin: frozenset[str]

    def __post_init__(self) -> None:
        if not self.builtin <= self.enabled:
            raise ValueError(
                "builtin must be a subset of enabled; not enabled: "
                f"{sorted(self.builtin - self.enabled)}"
            )

    def is_enabled(self, symbol: str) -> bool:
        return symbol in self.enabled

    def is_builtin(self, symbol: str) -> bool:
        return symbol in self.builtin

    @property
    def is_degenerate(self) -> bool:
        return not self.enabled


def parse_kernel_config(data: bytes) -> KernelConfig:
    """Parse ``.config`` bytes into a :class:`KernelConfig` of enabled and built-in symbols."""
    text = data.decode("utf-8", "replace")
    enabled: set[str] = set()
    builtin: set[str] = set()
    for line in text.splitlines():
        match = _ENABLED.match(line.strip())
        if match is None:
            continue
        enabled.add(match.group(1))
        if match.group(2) == "y":
            builtin.add(match.group(1))
    return KernelConfig(enabled=frozenset(enabled), builtin=frozenset(builtin))
