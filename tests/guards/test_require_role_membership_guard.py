"""Guard: no ``require_role`` reaches its non-member arm unenveloped (ADR-0507, #1681).

:func:`~kdive.security.authz.rbac.require_role` raises from **two** sites, and only one of them
is owned by a boundary. The rank-below site raises :class:`RoleDenied`, which
``DenialAuditMiddleware`` catches, audits, and envelopes. The *non-member* site — ``project not
in ctx.projects`` — raises the bare :class:`AuthorizationError`, which
``middleware/denial_audit.py``'s ``_DENIAL_TYPES`` deliberately does not own. A bare
``AuthorizationError`` that escapes a handler therefore reaches the client as a raw
``ToolError`` instead of an ``authorization_denied`` envelope. That was #1661, and after
ADR-0493 §2 deleted ``UsageTrackingMiddleware``'s ``except AuthorizationError`` arm the failure
mode is *silent*: nothing meters it, nothing audits it, and nothing in ``just ci`` fails it.

ADR-0493 disclosed the absence of this guard as a standing residual and recorded that its 52-site
audit was point-in-time, not an invariant. This module is the invariant.

**The rule.** Every ``require_role`` call must be *membership-covered*: by the time control
reaches it, ``project in ctx.projects`` must already be established, or the resulting
``AuthorizationError`` must be caught and enveloped. Four ways to satisfy that, checked in
:func:`_coverage`; a site satisfying none is a bare-``AuthorizationError`` escape and fails.

Why the rule is universal rather than scoped to caller-named projects. #1681 framed the hazard as
a *caller-supplied* project identifier, on the reasoning that a row-resolved one is safe. It is
not safe by itself — a row fetched by id belongs to whatever project owns it, which need not be
one of the caller's. What actually makes the row-resolving tools safe is that they pair the fetch
with an explicit ``row.project not in ctx.projects`` check (the dominant idiom on this surface,
e.g. ``lifecycle/runs/cancel.py``), and that check is exactly rule 1. So covering every call site
costs no allowlist entries over covering only the caller-named ones, and it closes the
row-resolved hazard too. Precision comes from the mitigations being real, not from narrowing
which sites are asked.

**What this analysis cannot resolve.** It is intra-procedural plus one level of intra-module call
graph. It cannot follow a project argument across two frames, across modules, or through a
container, and it does not know that a repository ``get`` is membership-scoped. Those sites are
not silently passed — they fail, and clearing one requires an entry in
:data:`_UNCOVERED_REQUIRE_ROLE` stating the cross-frame fact the analysis could not see. The
allowlist is where this guard's dataflow limits are written down, per site, in prose. ADR-0507
records the tradeoff.

Scope boundary. ``test_denial_envelope_actions.py`` walks ``ToolResponse.denied(...)`` calls —
envelope *construction*. A bare ``AuthorizationError`` builds no envelope at all, so it is
invisible there; that is the gap this module fills, and the two do not overlap.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "kdive"

_REQUIRE_ROLE = "require_role"
_REQUIRE_PLATFORM_ROLE = "require_platform_role"
_REQUIRE_PROJECT = "require_project"
_PROJECTS_ATTR = "projects"
_BASE_ERROR = "AuthorizationError"

# Sites the analysis cannot clear on its own, each with the cross-frame fact that makes it safe.
# Keyed by `<module>::<enclosing function>` rather than by line so an unrelated edit above the
# call does not churn this table. Mirrors `_ROLELESS_DENIALS`'s dict-with-reason style in
# `test_denial_envelope_actions.py`: a new entry is a deliberate diff, reviewed as a claim about
# reachability, so "I forgot the membership check" cannot ship as a one-line allowlist bump.
_UNCOVERED_REQUIRE_ROLE: dict[str, str] = {
    "services/investigations/lifecycle.py::_require_admin_for_force": (
        "`project` arrives two frames up: `close_investigation` resolves the Investigation row "
        "and checks membership, then passes `project` through `_couple_bound_systems`. The "
        "analysis follows one call level, not two. The local `except RoleDenied` deliberately "
        "does not widen to the base class — a non-member here is unreachable, and catching it "
        "would convert it into FORCE_REQUIRES_ADMIN, which is the wrong answer for a non-member"
    ),
}

# `require_platform_role` carries no project, so membership is not the question — but it raises
# the same bare `AuthorizationError`, and `middleware/denial_audit.py` states the invariant that
# every such denial "is audited by its own handler and must pass through here untouched"
# (ADR-0043 §4). Nothing checked that. An uncaught one is the identical raw-`ToolError` escape.
_UNENVELOPED_PLATFORM_ROLE: dict[str, str] = {}


@dataclass(frozen=True)
class _Site:
    """One authorization call site, located for reporting."""

    module: str
    function: str
    line: int

    @property
    def key(self) -> str:
        return f"{self.module}::{self.function}"

    def __str__(self) -> str:
        return f"{self.module}:{self.line} in {self.function}()"


def _call_name(node: ast.Call) -> str | None:
    """The bare name of the function being called, through an attribute access."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def _is_ctx_projects(node: ast.expr) -> bool:
    """True for ``<name>.projects`` — the membership set on ``RequestContext``.

    Requires a plain name as the base so ``self.config.projects`` or a row's own ``.projects``
    cannot pass as a membership check. ``ctx.roles`` is deliberately *not* accepted: a project
    may be in ``ctx.projects`` with no role, so ``roles`` is neither necessary nor sufficient
    for membership.
    """
    return (
        isinstance(node, ast.Attribute)
        and node.attr == _PROJECTS_ATTR
        and isinstance(node.value, ast.Name)
    )


def _handles_base_error(handler: ast.ExceptHandler) -> bool:
    """True if ``handler`` catches the base ``AuthorizationError`` (or catches everything).

    ``except RoleDenied`` does **not** count. ``RoleDenied`` is the arm a boundary already owns;
    catching only it leaves the non-member arm — the one that escapes — still propagating.
    """
    caught = handler.type
    if caught is None:
        return True
    parts = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    return any(
        (part.attr if isinstance(part, ast.Attribute) else getattr(part, "id", None)) == _BASE_ERROR
        for part in parts
    )


def _enveloped_by_enclosing_try(function: ast.AST, line: int) -> bool:
    """True if ``line`` sits in the body of a ``try`` in ``function`` that catches the base error.

    The ``try`` body only — a call in an ``except``/``else``/``finally`` clause is not protected
    by that statement's own handlers.
    """
    for node in ast.walk(function):
        if not isinstance(node, ast.Try):
            continue
        body_start = node.body[0].lineno
        body_end = max(stmt.end_lineno or stmt.lineno for stmt in node.body)
        if body_start <= line <= body_end and any(map(_handles_base_error, node.handlers)):
            return True
    return False


def _membership_established_before(function: ast.AST, line: int) -> bool:
    """True if ``function`` establishes membership at a statement preceding ``line``.

    Either an explicit ``require_project(ctx, ...)`` — which raises ``ProjectMembershipDenied``,
    a class the dispatch boundary *does* envelope — or an ``in``/``not in`` test against
    ``ctx.projects``, which is how the row-resolving tools gate before they authorize.
    """
    for node in ast.walk(function):
        if getattr(node, "lineno", line) >= line:
            continue
        if isinstance(node, ast.Call) and _call_name(node) == _REQUIRE_PROJECT:
            return True
        if (
            isinstance(node, ast.Compare)
            and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
            and any(map(_is_ctx_projects, node.comparators))
        ):
            return True
    return False


def _iterates_ctx_projects(function: ast.AST, project_arg: ast.expr | None) -> bool:
    """True if the project argument is a name bound by iterating ``ctx.projects``.

    ``for project in ctx.projects: require_role(ctx, project, ...)`` cannot reach the non-member
    arm — every value it can take is already a membership. This is how ``projects_with_role``
    and the granted-set readers enumerate.
    """
    if not isinstance(project_arg, ast.Name):
        return False
    for node in ast.walk(function):
        if not isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            continue
        target = node.target
        if (
            isinstance(target, ast.Name)
            and target.id == project_arg.id
            and _is_ctx_projects(node.iter)
        ):
            return True
    return False


def _project_argument(node: ast.Call) -> ast.expr | None:
    """The ``project`` argument of a ``require_role`` call, positional or keyword."""
    for keyword in node.keywords:
        if keyword.arg == "project":
            return keyword.value
    return node.args[1] if len(node.args) > 1 else None


class _AuthzCallCollector(ast.NodeVisitor):
    """Collect authorization call sites, and every intra-module call, with their enclosing scope.

    ``self.enclosing`` is the innermost function containing the node, which is what every
    coverage rule is evaluated against.
    """

    def __init__(self, module: str) -> None:
        self.module = module
        self.enclosing: ast.FunctionDef | ast.AsyncFunctionDef | None = None
        self.role_calls: list[tuple[_Site, ast.AST, ast.expr | None]] = []
        self.platform_calls: list[tuple[_Site, ast.AST]] = []
        # Callee name -> the (enclosing function, line) of each call to it in this module.
        self.calls_by_callee: dict[str, list[tuple[ast.AST | None, int]]] = {}

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        outer, self.enclosing = self.enclosing, node
        self.generic_visit(node)
        self.enclosing = outer

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        if name is not None:
            self.calls_by_callee.setdefault(name, []).append((self.enclosing, node.lineno))
        if self.enclosing is not None:
            site = _Site(self.module, self.enclosing.name, node.lineno)
            if name == _REQUIRE_ROLE:
                self.role_calls.append((site, self.enclosing, _project_argument(node)))
            elif name == _REQUIRE_PLATFORM_ROLE:
                self.platform_calls.append((site, self.enclosing))
        self.generic_visit(node)


def _caller_covered(collector: _AuthzCallCollector, function: ast.AST) -> bool:
    """True if every intra-module call to ``function`` is itself membership-covered.

    One level of call graph, which is what the two granted-set readers and the artifact fetchers
    need: ``accounting/reports.py``'s ``_resolve_granted_set`` raises deliberately and its sole
    caller wraps it in ``try/except AuthorizationError`` (ADR-0493), and
    ``raw_fetch.py``'s ``_resolve_key`` is called only after its caller has checked
    ``run.project not in ctx.projects``.

    Requires at least one call site: a function with none is unreachable from this module and
    proves nothing, so it stays uncovered rather than passing vacuously.
    """
    call_sites = collector.calls_by_callee.get(getattr(function, "name", ""), [])
    if not call_sites:
        return False
    return all(
        caller is not None
        and (
            _enveloped_by_enclosing_try(caller, line)
            or _membership_established_before(caller, line)
        )
        for caller, line in call_sites
    )


def _coverage(
    collector: _AuthzCallCollector,
    function: ast.AST,
    line: int,
    project_arg: ast.expr | None,
) -> str | None:
    """The rule that covers this ``require_role`` site, or None if none does."""
    if _iterates_ctx_projects(function, project_arg):
        return "iterates ctx.projects"
    if _membership_established_before(function, line):
        return "membership checked before the call"
    if _enveloped_by_enclosing_try(function, line):
        return "enclosing except AuthorizationError"
    if _caller_covered(collector, function):
        return "every caller in this module is covered"
    return None


def _collect(path: Path) -> _AuthzCallCollector:
    collector = _AuthzCallCollector(path.relative_to(_SRC_ROOT).as_posix())
    collector.visit(ast.parse(path.read_text(encoding="utf-8")))
    return collector


def _modules() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def test_the_guard_sees_the_authorization_surface() -> None:
    """The walk finds both call families — an empty walk would pass every other test vacuously.

    A rename, a package move, or a broken ``_SRC_ROOT`` would otherwise turn this whole module
    into a no-op that still reports green.
    """
    role_sites = [site for path in _modules() for site, _, _ in _collect(path).role_calls]
    platform_sites = [site for path in _modules() for site, _ in _collect(path).platform_calls]
    assert len(role_sites) >= 40, f"require_role walk found only {len(role_sites)} sites"
    assert len(platform_sites) >= 20, (
        f"require_platform_role walk found only {len(platform_sites)} sites"
    )


def test_every_require_role_is_membership_covered() -> None:
    """No ``require_role`` can reach its non-member arm with nothing to envelope it (#1661)."""
    uncovered: dict[str, str] = {}
    for path in _modules():
        collector = _collect(path)
        for site, function, project_arg in collector.role_calls:
            if _coverage(collector, function, site.line, project_arg) is None:
                uncovered[site.key] = str(site)

    unexpected = {
        key: where for key, where in uncovered.items() if key not in _UNCOVERED_REQUIRE_ROLE
    }
    assert not unexpected, (
        "require_role can raise the bare AuthorizationError here with no membership check and "
        "no handler that envelopes it, so the caller gets a raw ToolError instead of an "
        "authorization_denied envelope (#1661, ADR-0493, ADR-0507). Add require_project(ctx, "
        "project) or a `project not in ctx.projects` check before the call, or catch "
        "AuthorizationError and return ToolResponse.denied(...). If the project provably cannot "
        "be a non-membership, record why in _UNCOVERED_REQUIRE_ROLE. Uncovered: "
        f"{sorted(unexpected.values())}"
    )

    stale = set(_UNCOVERED_REQUIRE_ROLE) - set(uncovered)
    assert not stale, (
        "these _UNCOVERED_REQUIRE_ROLE entries no longer name an uncovered site — the code now "
        f"carries a real membership check, so drop the entry: {sorted(stale)}"
    )


def test_every_require_platform_role_denial_is_enveloped() -> None:
    """A platform-role denial is enveloped by its own handler (ADR-0043 §4).

    ``DenialAuditMiddleware`` owns only ``RoleDenied`` and ``ProjectMembershipDenied`` and
    documents that every other ``AuthorizationError`` "is audited by its own handler and must
    pass through here untouched". An uncaught ``require_platform_role`` breaks that premise the
    same way #1661 did.
    """
    unenveloped: dict[str, str] = {}
    for path in _modules():
        collector = _collect(path)
        for site, function in collector.platform_calls:
            if not _enveloped_by_enclosing_try(function, site.line) and not _caller_covered(
                collector, function
            ):
                unenveloped[site.key] = str(site)

    unexpected = {
        key: where for key, where in unenveloped.items() if key not in _UNENVELOPED_PLATFORM_ROLE
    }
    assert not unexpected, (
        "require_platform_role raises the bare AuthorizationError, which no middleware "
        "envelopes, so this denial reaches the client as a raw ToolError (ADR-0043 §4, "
        "ADR-0507). Wrap the call in try/except AuthorizationError and return "
        f"ToolResponse.denied(...). Unenveloped: {sorted(unexpected.values())}"
    )

    stale = set(_UNENVELOPED_PLATFORM_ROLE) - set(unenveloped)
    assert not stale, (
        f"these _UNENVELOPED_PLATFORM_ROLE entries no longer name an unenveloped site: "
        f"{sorted(stale)}"
    )
