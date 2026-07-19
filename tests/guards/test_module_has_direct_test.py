"""Guard: every ``src/kdive`` module is imported by dotted path in at least one test (#665).

**The invariant.** A mutant in a module that no test imports *directly* survives
unattributably: mutmut maps a killing test to a mutant by running the suite against it, so a
module that is only exercised behaviourally through cross-file tests — never itself imported into
a test's runtime import graph — has no test whose failure can be attributed to it. So every source
module must be mirrored by a test that imports it by its real dotted path. This "no direct unit
test" bucket originates in #665 and is tracked in ``docs/development/mutation-sweep-status.md`` — it
was closed there, silently reopened by post-sweep modules, and re-closed by #1298 / #1304.
Enforcing it here (the sweep doc calls the scan "reproducible") stops the bucket reopening
unnoticed.

It is *not* defined by ADR-0229 — that ADR only folds the mutmut env shims into the ``just mutate``
recipe. The issue title's "ADR-0229 scan" is a misnomer; the citation is #665 / the sweep doc.

**The scan.** An AST walk collects every ``kdive.*`` dotted path imported by any file under
``tests/`` — reproducing #665's scan — and fails a source module whose dotted name is absent.
"Any file under ``tests/``" is deliberate: a shared fixture or helper module (a ``conftest.py``,
``tests/deploy/grafana_catalog.py``, ...) that imports the module counts, because a test using that
fixture loads the module into its runtime import graph, which is the signal mutmut attributes on.
Two forms that do *not* load the module at runtime are excluded so they cannot mask a real gap:
imports under an ``if TYPE_CHECKING:`` guard (evaluated only by type checkers), and relative
imports (never used by a test here and never naming a ``kdive`` source module). One dynamic form
*is* counted so a mirror test may use it: ``importlib.import_module("kdive…")`` with a string
literal.

A source module fails unless its dotted name is in that set, with two exemptions:

- a package initializer (``__init__.py``) that defines no function or class. mutmut (3.6.0)
  mutates only code inside a top-level function or method, so such a module — a structural
  re-export / aggregator — has no mutant for a killing test to be attributed to. A logic-bearing
  ``__init__`` such as ``kdive.config`` *does* define functions, is therefore not exempt, and is
  covered by its own direct test; and
- an explicit, justified :data:`_ALLOWLIST` entry (protocol-only / typing-only modules that carry
  no unit-testable logic). Kept minimal — currently empty.

**Must run under the project interpreter.** The scan ``ast.parse``s every test and source file; an
interpreter that cannot parse the tree's syntax raises ``SyntaxError`` and errors this guard
loudly. ``just test`` runs it on the project's Python, so the guard never degrades to a partial
scan that silently passes.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "kdive"
_TESTS = _ROOT / "tests"

# Justified per-module exemptions: a module with a mutatable surface that legitimately has no
# direct mirror test (e.g. a protocol-only / typing-only module). Each entry needs a one-line
# reason. Keep this minimal — a genuinely untested behavioral module is a real gap to fix, not to
# allowlist. Currently empty: after #1298 / #1304 the only undirected modules are pure-aggregator
# ``__init__`` files, exempt by rule.
_ALLOWLIST: frozenset[str] = frozenset()


def _module_name(path: Path) -> str:
    """Dotted import name for a source file (``foo/__init__.py`` -> the package ``kdive.foo``)."""
    parts = list(path.relative_to(_SRC.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_type_checking_guard(test: ast.expr) -> bool:
    """True for the test of an ``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:`` block."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _record_import_module_call(call: ast.Call, out: set[str]) -> None:
    """Record an ``importlib.import_module("dotted.path")`` string-literal target."""
    func = call.func
    is_call = (isinstance(func, ast.Attribute) and func.attr == "import_module") or (
        isinstance(func, ast.Name) and func.id == "import_module"
    )
    if not (is_call and call.args and isinstance(call.args[0], ast.Constant)):
        return
    target = call.args[0].value
    if isinstance(target, str):
        out.add(target)


def _collect_imports(node: ast.AST, out: set[str]) -> None:
    """Walk ``node``, recording runtime dotted-path imports; skip ``TYPE_CHECKING`` bodies.

    ``import a.b.c`` records ``a.b.c``; ``from a.b import c`` records both ``a.b`` and ``a.b.c`` so
    the submodule form matches whether ``c`` is a submodule or an attribute. Relative imports are
    ignored: no test in this tree uses them, and they never name a ``kdive`` source module.
    """
    if isinstance(node, ast.Import):
        out.update(alias.name for alias in node.names)
        return
    if isinstance(node, ast.ImportFrom):
        if node.level == 0 and node.module:
            out.add(node.module)
            out.update(f"{node.module}.{alias.name}" for alias in node.names)
        return
    if isinstance(node, ast.Call):
        _record_import_module_call(node, out)
    children = (
        node.orelse
        if isinstance(node, ast.If) and _is_type_checking_guard(node.test)
        else ast.iter_child_nodes(node)
    )
    for child in children:
        _collect_imports(child, out)


def _import_targets(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    _collect_imports(tree, out)
    return out


def _test_imports() -> set[str]:
    out: set[str] = set()
    for py in _TESTS.rglob("*.py"):
        out |= _import_targets(ast.parse(py.read_text(encoding="utf-8"), filename=str(py)))
    return out


def _defines_function_or_class(tree: ast.Module) -> bool:
    """True if the module defines a function or class anywhere — mutmut's only mutation surface."""
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for node in ast.walk(tree)
    )


def _is_pure_aggregator_init(path: Path, tree: ast.Module) -> bool:
    return path.name == "__init__.py" and not _defines_function_or_class(tree)


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
        "imports each module directly (a static import, or importlib.import_module with a string "
        "literal), or, only if it is genuinely unit-untestable (protocol-only / typing-only), add "
        "a justified entry to _ALLOWLIST:\n  " + "\n  ".join(offenders)
    )


# --- self-tests: lock the scan semantics the guard depends on -------------------------------------


def _targets(source: str) -> set[str]:
    return _import_targets(ast.parse(source))


def test_scan_records_plain_and_aliased_and_from_imports() -> None:
    targets = _targets("import a.b.c as x\nfrom p.q import r, s\n")
    assert {"a.b.c", "p.q", "p.q.r", "p.q.s"} <= targets


def test_scan_records_star_import_module() -> None:
    assert "p.q" in _targets("from p.q import *\n")


def test_scan_excludes_type_checking_only_imports() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from only.typed import Thing\n"
        "else:\n"
        "    from runtime.mod import real\n"
    )
    targets = _targets(source)
    assert "only.typed" not in targets
    assert "only.typed.Thing" not in targets
    assert "runtime.mod.real" in targets


def test_scan_records_importlib_string_literal() -> None:
    assert "a.b.c" in _targets("import importlib\nimportlib.import_module('a.b.c')\n")
    assert "d.e" in _targets("from importlib import import_module\nimport_module('d.e')\n")


def test_module_name_maps_package_init_to_package() -> None:
    assert _module_name(_SRC / "config" / "__init__.py") == "kdive.config"
    assert _module_name(_SRC / "config" / "manifest.py") == "kdive.config.manifest"


def test_aggregator_detection_distinguishes_defs_from_reexport() -> None:
    reexport = ast.parse('"""doc"""\nimport a.b\nfrom c import d\n__all__ = ["d"]\n')
    assert not _defines_function_or_class(reexport)
    # A pure-data module (no def/class) has no mutmut surface, even with a module-level call.
    assert not _defines_function_or_class(ast.parse("import a\nVALUE = a.compute()\n"))
    assert _defines_function_or_class(ast.parse("def f():\n    return 1\n"))
    assert _defines_function_or_class(ast.parse("class C:\n    x = 1\n"))
