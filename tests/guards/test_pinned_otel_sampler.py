"""Guard: no test module builds its own ``TracerProvider`` (#1693).

**The invariant.** ``TracerProvider()``'s default ``ParentBased(ALWAYS_ON)`` sampler honors
the sampled flag of whatever context is ambient when a span opens, so under
``--dist worksteal`` a span can be silently discarded because an *earlier, unrelated* test on
the same worker left a non-sampled parent context attached. The assertion downstream then
reports a missing span. #1683 hit this in one file and pinned that file's sampler; the same
unpinned default was live in eleven others (#1693).

Pinning eleven call sites fixes the eleven; it does nothing about the twelfth. So the
constructor call itself is what this guard removes from the test suite's vocabulary: every
test takes its provider from :func:`tests.support.otel.tracer_provider`, where the sampler is
pinned once. A new telemetry test cannot reintroduce the defect without tripping this walk,
which is the difference between the flake being absent and being unrepresentable.

**Scope.** ``tests/`` only. Production sampling is deliberately
``ParentBased(TraceIdRatioBased(0.1))`` (``kdive/observability/facade.py``) and is not a
determinism concern, so ``src/`` is out of scope by design rather than by omission.

**The escape hatch.** :data:`_ALLOWED` lists the two files that may name the constructor: the
factory that owns the pin, and the test that proves the unpinned default really does drop the
span. A future test of sampling behavior itself would add an entry here with its reason —
deliberately a visible, reviewed edit rather than a silent one.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]

# `tests/support/otel.py` is the single pinned definition site. `tests/support/test_otel.py`
# needs the unpinned default as its negative control: without constructing one it could not
# show that the pin is load-bearing rather than decorative.
_ALLOWED = {
    _TESTS_ROOT / "support" / "otel.py",
    _TESTS_ROOT / "support" / "test_otel.py",
}

_CONSTRUCTOR = "TracerProvider"


def _constructor_call_lines(tree: ast.AST) -> list[int]:
    """Lines of every ``TracerProvider(...)`` call, however the name was imported.

    Matches both the bare name and an attribute access (``trace.TracerProvider()``), and
    only in call position — a type annotation or an ``isinstance`` check names the class
    without building one and is not what this guard is about.
    """
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        named = (isinstance(func, ast.Name) and func.id == _CONSTRUCTOR) or (
            isinstance(func, ast.Attribute) and func.attr == _CONSTRUCTOR
        )
        if named:
            lines.append(node.lineno)
    return lines


def test_no_test_module_constructs_its_own_tracer_provider() -> None:
    offenders: list[str] = []
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        if path in _ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(
            f"{path.relative_to(_TESTS_ROOT.parent)}:{line}"
            for line in _constructor_call_lines(tree)
        )

    assert not offenders, (
        "these test modules build their own TracerProvider, whose default "
        "ParentBased(ALWAYS_ON) sampler drops spans under a leaked non-sampled ambient "
        "context (#1683/#1693); call tests.support.otel.tracer_provider() instead: "
        + ", ".join(offenders)
    )


def test_the_walk_would_catch_an_unpinned_constructor() -> None:
    """The scan finds a real call, so a green guard means "no offenders", not "no scan".

    A walk that silently matched nothing — a renamed constructor, an AST shape it does not
    handle — would pass the guard above forever. Feeding it the two shapes an offender can
    take proves the matcher still bites.
    """
    bare = ast.parse("from x import TracerProvider\nTracerProvider()\n")
    qualified = ast.parse("import x\nx.TracerProvider(sampler=None)\n")

    assert _constructor_call_lines(bare) == [2]
    assert _constructor_call_lines(qualified) == [2]
    assert _constructor_call_lines(ast.parse("def f(p: TracerProvider) -> None: ...\n")) == []


def test_the_allowlist_names_only_live_files() -> None:
    """A stale allowlist entry is a hole: it would exempt a path that no longer exists,
    and would keep exempting the name if a future file reappeared at it unreviewed."""
    missing = [str(path) for path in sorted(_ALLOWED) if not path.is_file()]
    assert not missing, f"allowlisted paths no longer exist: {missing}"


def test_the_allowlisted_factory_actually_constructs_one() -> None:
    """The exemption has to be load-bearing, not inherited from an earlier refactor.

    If ``tests/support/otel.py`` stopped building a provider, its allowlist entry would be
    dead weight that quietly widens the guard's blind spot.
    """
    factory = _TESTS_ROOT / "support" / "otel.py"
    tree = ast.parse(factory.read_text(encoding="utf-8"), filename=str(factory))
    assert _constructor_call_lines(tree), f"{factory} no longer constructs a TracerProvider"
