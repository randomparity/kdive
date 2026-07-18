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
- ``ubsan`` — the UBSAN sanitizer prints ``UBSAN: <check> in <file>:<line>:<col>`` for every
  report kind (``shift-out-of-bounds``, ``array-index-out-of-bounds``,
  ``signed-integer-overflow``, …); ``UBSAN:`` is the stable header prefix (ADR-0383, #1267).
"""

from __future__ import annotations

import re

#: The console crash-signature matcher shared by boot readiness and the ``watch_for_crash`` console
#: watch (#984, ADR-0367). A crash-window scan gate, distinct from the ``|``-OR literal presets
#: below (which drive ``expected_boot_failure``): this catches the common kernel crash headers the
#: readiness probe already keys off. Word boundaries keep the bare ``BUG:``/``Oops:`` tokens from
#: matching benign substrings (``DEBUG:``). ``UBSAN:`` joins ``KASAN:``/``KFENCE:`` here: all three
#: are non-fatal-by-default kernel sanitizer reports that a kernel-debugging platform treats as a
#: crash signal, because a sanitizer firing during a debug boot is the reproduction (ADR-0383).
_CRASH_SIGNATURE = re.compile(
    r"Kernel panic"
    r"|(?<![A-Za-z])BUG:"
    r"|(?<![A-Za-z])Oops:"
    r"|general protection fault"
    r"|[Uu]nable to handle kernel"
    r"|KASAN:"
    r"|KFENCE:"
    r"|UBSAN:"
    r"|detected stall"
)


def first_crash_signature(text: str) -> re.Match[str] | None:
    """Return the first kernel-crash-signature match in ``text``, or ``None``.

    The single source of truth for the crash-signature matcher shared by boot readiness and the
    ``watch_for_crash`` console watch (#984). ``match.group(0)`` is the matched literal (e.g.
    ``"Kernel panic"``, ``"KASAN:"``). Case-sensitive, word-boundaried where the bare tokens
    (``BUG:``/``Oops:``) would otherwise match benign substrings (``DEBUG:``).
    """
    return _CRASH_SIGNATURE.search(text)


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
    "ubsan": "UBSAN:",
}

#: Every console-text crash kind: the custom lane plus the presets. The boot matcher gates on
#: membership here so preset and custom expectations match identically.
CONSOLE_CRASH_KINDS: frozenset[str] = frozenset({CONSOLE_CRASH_KIND, *CRASH_SIGNATURE_PRESETS})
