"""Kernel symbols no config fragment can set, with the Kconfig line each was verified at.

The seed for invariant I1 of the clause model #1854 settles. A symbol here is unsettable **in
principle** - prompt-less, or existing only because something else ``select``s it - so ``make
olddefconfig``
discards it out of a fragment and any surface that names it hands the reader an instruction it
cannot follow. Two tests read this list, which is why it lives in a support module rather than
inside either of them:

- ``tests/kernel_config/test_requirements.py`` holds it out of every clause of every feature -
  ``advertised`` and ``gate_required`` alike.
- ``tests/mcp/resources/test_kernel_config_contract_docs.py`` bounds the allowlist that can
  silence the doc guard, so the cheapest way out of a doc failure is not to allowlist one of
  these and ship the defect.

**This is a regression guard, not a proof.** It catches the return of a symbol already known to
be unsettable and cannot catch a tenth: nothing in a ``.config`` distinguishes a prompt-less
symbol from a prompted one, so the only real check is a human reading Kconfig. The list grows as
symbols are verified; it is not a claim that every clause has been audited.

A symbol that is settable **once a prerequisite holds** does not belong here.
``SERIAL_8250_CONSOLE`` and ``DEBUG_INFO_BTF`` are real prompts that ``olddefconfig`` drops on an
unprepared config; #1854 keeps them as clause members and gives the prerequisite its own
clause.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

# symbol -> the Linux v7.0 Kconfig file:line it was read at. Cited so a failure can be checked
# against the kernel directly rather than taken on trust.
UNSETTABLE_SYMBOLS: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "TRACING": "kernel/trace/Kconfig:179",
        "DYNAMIC_FTRACE": "kernel/trace/Kconfig:301",
        "BPF_EVENTS": "kernel/trace/Kconfig:853",
        "LOCKDEP": "lib/Kconfig.debug:1591",
        "BPF": "kernel/bpf/Kconfig:4",
        "UPROBES": "arch/Kconfig:182",
        "DEBUG_INFO": "lib/Kconfig.debug:249",
        "KEXEC_CORE": "kernel/Kconfig.kexec:11",
        "VMCORE_INFO": "kernel/Kconfig.kexec:8",
    }
)

# The six symbols #1854 names explicitly, because the registry's own comments already called
# them unsettable. Both readers assert this subset, so emptying or thinning the list to get a
# green suite fails instead of passing quietly.
I1_SEED: Final[frozenset[str]] = frozenset(
    {"KEXEC_CORE", "VMCORE_INFO", "DEBUG_INFO", "BPF_EVENTS", "TRACING", "DYNAMIC_FTRACE"}
)
