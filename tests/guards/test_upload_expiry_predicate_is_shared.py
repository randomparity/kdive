"""Guard: the upload-window expiry rule is compared in exactly one place (ADR-0512, #1555).

The two finalize lanes each wrote their own comparison of a ``ManifestStamp``'s ``deadline``
against its ``server_time``, and wrote them as exact logical negations — ``deadline <
server_time`` in the runs service, ``deadline >= server_time`` in the investigations tool. They
agreed, so no test could tell them apart; the cost was that any later change to the rule (a grace
period, strictness at equality, a skew allowance) would land on one spelling only.

ADR-0512 hoisted the comparison to ``ManifestStamp.expired``. This guard is what stops the
hand-rolled spelling from coming back: an AST walk over ``src/kdive/`` fails any comparison that
puts a ``.deadline`` on one side and a ``.server_time`` on the other, outside the module that owns
the predicate.

It matches on the *attribute pair*, not on a receiver name, so it catches the comparison however
the stamp is spelled — ``stamp.deadline < stamp.server_time``, ``exc.stamp.deadline <
now.server_time``, or a swapped-operand rewrite.

Two things it does **not** reach, stated so the guard is not over-trusted:

- **Comparisons through local aliases.** ``d, s = stamp.deadline, stamp.server_time`` followed by
  ``if d < s:`` reads no attribute inside the ``Compare`` node, so it passes. Catching that needs
  dataflow, not an AST shape match. The guard raises the cost of the duplication; it does not make
  it impossible.
- **A comparison against a Python-side clock.** ``deadline < datetime.now(UTC)`` is a different
  defect (ADR-0444's "measure against the Postgres clock"), argued in the lanes' own docstrings,
  and it names only one of the two fields so it is not this rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

from kdive.artifacts.uploads.upload_manifest import ManifestStamp

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "kdive"

# The module that owns `ManifestStamp` and therefore owns the one comparison. The whole package is
# walked rather than just the two lanes: an expiry verdict rendered in a reconciler or a provider
# module would be the same drift, and scoping the walk to where the duplication happened to live
# would leave the guard blind to the next site.
_ALLOWED = _SRC_ROOT / "artifacts" / "upload_manifest.py"

_FIELDS = frozenset(ManifestStamp._fields)


def test_the_stamp_fields_this_guard_matches_are_the_real_ones() -> None:
    """The guard's field names come from ``ManifestStamp`` itself, so a rename cannot blind it.

    Matching hardcoded ``"deadline"``/``"server_time"`` strings would leave the guard silently
    passing over a renamed field while the duplication it exists to catch returned underneath.
    """
    assert {"deadline", "server_time"} == _FIELDS


def _attribute_name(node: ast.expr) -> str | None:
    """The attribute being accessed, if ``node`` reads one of the stamp's two fields."""
    if isinstance(node, ast.Attribute) and node.attr in _FIELDS:
        return node.attr
    return None


def _compares_the_stamp_fields(node: ast.Compare) -> bool:
    """True if this comparison puts one stamp field against the other, in either order."""
    names = [_attribute_name(operand) for operand in [node.left, *node.comparators]]
    present = {name for name in names if name is not None}
    return present == _FIELDS


def test_no_module_hand_rolls_the_expiry_comparison() -> None:
    """Only ``upload_manifest`` compares a manifest deadline against its reference clock."""
    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path == _ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [
            f"{path.relative_to(_SRC_ROOT.parents[1])}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare) and _compares_the_stamp_fields(node)
        ]
    assert not offenders, (
        "an upload-window expiry verdict is being computed outside ManifestStamp.expired, which "
        "is how the two finalize lanes came to spell one rule two ways (#1555); use "
        f"`stamp.expired` at: {offenders}"
    )


def test_the_guard_detects_the_spelling_it_exists_to_ban() -> None:
    """Both historical spellings, and a swapped-operand rewrite, trip the matcher.

    Without this the guard could pass vacuously — an AST walk that matches nothing looks
    identical to a clean tree.
    """
    banned = (
        "stamp.deadline < stamp.server_time",  # the runs service's pre-hoist spelling
        "stamp.deadline >= stamp.server_time",  # the investigations tool's, its exact negation
        "stamp.server_time > stamp.deadline",  # the same verdict, operands swapped
        "exc.stamp.deadline < clock.server_time",  # fields reached through different receivers
    )
    for source in banned:
        node = ast.parse(source, mode="eval").body
        assert isinstance(node, ast.Compare)
        assert _compares_the_stamp_fields(node), f"guard missed the banned spelling: {source}"


def test_the_guard_ignores_comparisons_that_are_not_the_expiry_rule() -> None:
    """A comparison naming only one of the two fields is not the rule and must not be flagged.

    Re-reading a manifest's deadline to detect a re-mint (ADR-0448 §2) compares two *deadlines*,
    and the reaper compares a deadline against a bound; neither is a verdict on expiry, and
    flagging them would push authors to suppress the guard rather than heed it.
    """
    allowed = (
        "before.deadline != after.deadline",  # the re-mint check
        "manifest.deadline < cutoff",
        "stamp.server_time > started_at",
    )
    for source in allowed:
        node = ast.parse(source, mode="eval").body
        assert isinstance(node, ast.Compare)
        assert not _compares_the_stamp_fields(node), f"guard over-matched: {source}"
