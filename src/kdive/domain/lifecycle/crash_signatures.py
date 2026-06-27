"""Named crash-signature presets for ``expected_boot_failure`` (ADR-0266, #865).

Each preset expands to a canonical ``|``-OR of case-sensitive literal substrings — the same
``parse_literal_terms`` lane the ``console_crash`` pattern uses (ADR-0064/0225): tested
line-by-line against the redacted boot-window console, ≤16 terms, ≤256 chars. Terms were chosen
against current kernel sources (``kernel/panic.c``, ``kernel/hung_task.c``, ``arch/x86/mm/fault.c``)
and the older EL kernels this project targets, so a preset matches across kernel versions and the
common arches:

- ``panic`` — ``panic()`` prints ``Kernel panic - not syncing: …``; the ``Kernel panic`` substring
  catches every panic wording.
- ``oops`` — the ``__die("Oops", …)`` header (``Oops: 0000 [#1]``, plus arm64
  ``Internal error: Oops:``), the modern x86 page-fault/NULL-deref wording (v5.0+), the pre-v5.0
  ``BUG: unable to handle kernel …`` form (EL8's 4.18), and ``BUG()``/``BUG_ON`` →
  ``kernel BUG at``.
- ``hung_task`` — khungtaskd prints ``INFO: task <c>:<p> blocked for more than <n> seconds.`` plus
  the ``hung_task_timeout_secs`` help line; ``INFO: task `` is the stable prefix across the
  standard, mutex-blocker, and newer ``blocked in I/O wait`` variants.
"""

from __future__ import annotations

#: The custom-pattern kind: the caller supplies the literal ``pattern`` verbatim.
CONSOLE_CRASH_KIND = "console_crash"

#: Preset name -> canonical ``|``-OR literal pattern. The single source of truth; the domain
#: model resolves a preset to its pattern at validation and the boot matcher searches the result.
CRASH_SIGNATURE_PRESETS: dict[str, str] = {
    "panic": "Kernel panic",
    "oops": (
        "Oops:"
        "|BUG: unable to handle page fault for address"
        "|BUG: kernel NULL pointer dereference"
        "|BUG: unable to handle kernel"
        "|kernel BUG at"
    ),
    "hung_task": "INFO: task |blocked for more than|blocked in I/O wait|hung_task",
}

#: Every console-text crash kind: the custom lane plus the presets. The boot matcher gates on
#: membership here so preset and custom expectations match identically.
CONSOLE_CRASH_KINDS: frozenset[str] = frozenset({CONSOLE_CRASH_KIND, *CRASH_SIGNATURE_PRESETS})
