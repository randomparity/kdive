"""Strip ANSI SGR escape codes from captured CLI/help text (issue #1891).

Python 3.14's ``argparse`` colours its ``--help`` output whenever the environment asks for
colour (``FORCE_COLOR``, a real TTY, etc.), and it colours a flag and its metavar in
*different* SGR spans::

    \x1b[1;36m--reason\x1b[0m \x1b[1;33mREASON\x1b[0m

A literal substring check like ``"--reason REASON" in help_text`` sits astride that escape
sequence and never matches once colour is on, even though the rendered text reads exactly
the same to a human. CI has no TTY and sets no ``FORCE_COLOR``, so this is invisible there;
it only reddens on a developer's or agent's shell that exports ``FORCE_COLOR``/``COLORTERM``,
on a diff that never touched the CLI.

This is the same defect class as #1883's ``chart-version-check`` (colourised ``uv version
--short`` breaking a shell string compare) — a guard comparing against text whose colouring
it does not control. That fix (PR #1887) stripped escapes with a ``sed`` pattern in the
justfile recipe; this is the Python-side equivalent for test assertions against captured
help text.

Guards built on this module must strip *before* asserting, on both directions: colour can
only make a `not in` check spuriously pass (never spuriously fail), which is the more
dangerous direction — see ``tests/test_ansi.py`` for the mutation proof that each stripped
`not in` guard still reddens when the forbidden text is genuinely present.
"""

from __future__ import annotations

import re

_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Return ``text`` with ANSI SGR colour escapes removed.

    A no-op on text that never carried escapes, so it is safe to apply defensively to any
    string a help-text-style assertion compares against, whether or not that particular
    string's rendering path is known to colourise today.
    """
    return _ANSI_SGR_RE.sub("", text)
