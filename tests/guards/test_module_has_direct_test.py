"""Guard: every ``src/kdive`` module is imported by dotted path in at least one test (#665).

**The invariant.** A mutant in a module that no test imports *directly* survives
unattributably: even when cross-file tests exercise the module's behavior, mutmut cannot map a
killing test back to it. So every source module must be mirrored by a test that imports it by its
real dotted path. This "no direct unit test" bucket originates in #665 and is tracked in
``docs/development/mutation-sweep-status.md`` — it was closed there, silently reopened by
post-sweep modules, and re-closed by #1298 / #1304. Enforcing it here stops the bucket reopening
unnoticed (the sweep doc calls the scan "reproducible"; this runs it in CI).

It is *not* defined by ADR-0229 — that ADR only folds the mutmut env shims into the ``just mutate``
recipe. The issue title's "ADR-0229 scan" is a misnomer; the citation is #665 / the sweep doc.

**The scan.** An AST walk collects every ``kdive.*`` dotted path imported by any file under
``tests/`` (``import a.b.c`` and both halves of ``from a.b import c``). A source module fails
unless its dotted name is in that set. Two exemptions, both because there is nothing for mutmut to
attribute a killing test to:

- a package initializer (``__init__.py``) that defines no function or class — a pure
  re-export / aggregator with no mutatable surface (a logic-bearing ``__init__`` such as
  ``kdive.config`` is *not* exempt and is covered by its own direct test); and
- an explicit, justified :data:`_ALLOWLIST` entry (protocol-only / typing-only or entrypoint
  modules that carry no unit-testable logic). Kept minimal — currently empty.

**Must run under the project interpreter.** The scan ``ast.parse``s every test file; an older
interpreter that cannot parse the tree's syntax would silently miss imports and flag covered
modules. ``just test`` runs this on the project's Python, so a parse failure surfaces loudly
rather than degrading the guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "kdive"
_TESTS = _ROOT / "tests"

# Justified per-module exemptions: a module with a mutatable surface that legitimately has no
# direct mirror test (e.g. a protocol-only / typing-only module, or an entrypoint carrying no
# unit-testable logic). Each entry needs a one-line reason. Keep this minimal — a genuinely
# untested behavioral module is a real gap to fix, not to allowlist. Currently empty: after
# #1298 / #1304 the only undirected modules are pure-aggregator ``__init__`` files, exempt by rule.
_ALLOWLIST: frozenset[str] = frozenset()


def _module_name(path: Path) -> str:
    """Dotted import name for a source file (``foo/__init__.py`` -> the package ``kdive.foo``)."""
    parts = list(path.relative_to(_SRC.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _import_targets(tree: ast.Module) -> set[str]:
    """Every absolute dotted module path a file could be importing.

    ``import a.b.c`` yields ``a.b.c``; ``from a.b import c`` yields both ``a.b`` and ``a.b.c`` so
    the submodule form matches whether ``c`` is a submodule or an attribute. Relative imports are
    ignored: no test in this tree uses them, and they never name a ``kdive`` source module.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module)
            out.update(f"{node.module}.{alias.name}" for alias in node.names)
    return out


def _test_imports() -> set[str]:
    out: set[str] = set()
    for py in _TESTS.rglob("*.py"):
        out |= _import_targets(ast.parse(py.read_text(encoding="utf-8"), filename=str(py)))
    return out


def _is_pure_aggregator_init(path: Path, tree: ast.Module) -> bool:
    if path.name != "__init__.py":
        return False
    return not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for node in ast.walk(tree)
    )


def _undirected_modules() -> list[str]:
    imported = _test_imports()
    offenders: list[str] = []
    for py in sorted(_SRC.rglob("*.py")):
        name = _module_name(py)
        if name in imported or name in _ALLOWLIST:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        if _is_pure_aggregator_init(py, tree):
            continue
        offenders.append(name)
    return offenders


def test_every_module_has_a_direct_test() -> None:
    offenders = _undirected_modules()
    assert not offenders, (
        "these src/kdive modules have no test that imports them by dotted path, so mutmut cannot "
        "attribute a killing test to them (the 'no direct unit test' invariant, #665 / "
        "docs/development/mutation-sweep-status.md — NOT ADR-0229). Add a mirror unit test that "
        "imports each module directly, or, only if it is genuinely unit-untestable (protocol-only "
        "/ typing-only), add a justified entry to _ALLOWLIST:\n  " + "\n  ".join(offenders)
    )
