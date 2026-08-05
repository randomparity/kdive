"""Behavioural tests for :mod:`tests.ansi` (issue #1891).

``strip_ansi`` exists because a coloured ``--help`` render splits a flag from its metavar
with an escape sequence, breaking a literal substring assertion. These tests prove the
function itself, plus the specific danger the issue called out: a `not in` guard is the
riskier direction, because colour can only make it spuriously *pass* (never spuriously
fail) — so a guard that forgets to strip can go quietly vacuous instead of red.
"""

from __future__ import annotations

from tests.ansi import strip_ansi


def test_strip_ansi_is_a_no_op_on_plain_text() -> None:
    plain = "--reason REASON    Mandatory non-blank break-glass justification (audited)."
    assert strip_ansi(plain) == plain


def test_strip_ansi_reassembles_a_colour_split_flag_and_metavar() -> None:
    """The exact split observed in the #1891 reproduction under Python 3.14 + FORCE_COLOR."""
    colored = "\x1b[1;36m--reason\x1b[0m \x1b[1;33mREASON\x1b[0m"
    assert strip_ansi(colored) == "--reason REASON"


def test_strip_ansi_handles_multiple_spans_in_one_string() -> None:
    colored = "\x1b[1;34musage: \x1b[0m\x1b[1;35mkdivectl images extend\x1b[0m [\x1b[32m-h\x1b[0m]"
    assert strip_ansi(colored) == "usage: kdivectl images extend [-h]"


def test_strip_ansi_reveals_a_colour_split_forbidden_token() -> None:
    """Mutation proof for the ``"GENARG_" not in help_text`` guard (test_dispatch_wiring.py).

    ``GENARG_`` is an internal placeholder that must never leak into rendered help; the
    guard is a `not in` check, so it can only fail safe by actually finding the token.
    Splitting the token itself across an escape sequence — the same shape argparse produces
    for a flag/metavar pair — proves that were it ever really present, stripping first still
    surfaces it as a literal substring rather than hiding it behind a colour code.
    """
    colored = "\x1b[1;36mGEN\x1b[0mARG_image_id"
    assert "GENARG_" in strip_ansi(colored)


def test_strip_ansi_reveals_a_colour_split_forbidden_phrase() -> None:
    """Mutation proof for the ``"remaining finalized pods" not in help_text`` guard
    (test_worker_death_settings.py). The real ``.help`` string is a plain attribute that
    never flows through argparse's colourising formatter, so it cannot be forced into
    colour via the environment the way `--help` output can; this constructs the shape a
    colourised copy would take instead, so the guard's `not in` check is proven non-vacuous
    independent of whether that rendering path ever changes.
    """
    colored = "remaining \x1b[31mfinalized\x1b[0m pods"
    assert "remaining finalized pods" in strip_ansi(colored)
