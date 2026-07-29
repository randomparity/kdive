"""Guard: a denial envelope never steers the caller at the tool that denied it (ADR-0471).

An ``authorization_denied`` envelope used to set ``suggested_next_actions=[<the denied tool>]``
at 21 sites, so an agent following the navigation contract was steered straight back into a call
it cannot complete without a grant it does not hold (#1596). ADR-0471 replaced every hand-rolled
denial with :meth:`ToolResponse.denied`, whose breadcrumb is the fixed
:data:`~kdive.mcp.responses.DENIAL_NEXT_ACTIONS` constant and which accepts no caller-supplied
actions at all.

Two independent layers, because either alone can rot:

1. **Source (this module's AST walk).** No call under ``src/kdive/`` may pass
   ``ErrorCategory.AUTHORIZATION_DENIED`` to anything. ``responses.py`` — where ``denied`` builds
   the envelope — is the single allowlisted definition site. A new tool that hand-rolls
   ``ToolResponse.failure(obj, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[_TOOL])``
   trips this immediately.
2. **Registry (the breadcrumb-is-public assertions).** Every name the constant carries must be a
   live, *public* tool. Only a **gated** tool can raise an authorization denial in the first
   place, so a breadcrumb drawn exclusively from ``PUBLIC_TOOLS`` can never name its own denier —
   the invariant holds by construction, not by per-site vigilance.

Scope boundary. ``tests/mcp/core/test_next_actions_graph.py`` guards the *doc-encoded* golden
path; this module guards the runtime ``suggested_next_actions`` of the denial envelope, which
that module explicitly places out of its own scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

from kdive.mcp.exposure import CLASSIFIED_TOOLS, PUBLIC_TOOLS
from kdive.mcp.responses import DENIAL_NEXT_ACTIONS, MISSING_ROLES_KEY, ToolResponse
from kdive.security.authz.rbac import PlatformRole, Role

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "kdive"

# The whole package, not just `mcp/`: a denial envelope built in a service or provider module
# would be just as wrong, and scoping the walk to where the defect happened to live would leave
# the guard blind to the next one.
#
# The one module allowed to name the category in a call: `ToolResponse.denied` builds the
# envelope by delegating to `cls.failure(..., ErrorCategory.AUTHORIZATION_DENIED, ...)`.
# `domain/errors.py` defines the member and the ADR-0123 suppression map, but does so in an
# enum body and a dict literal — neither is a call, so it needs no allowlist entry.
_ALLOWED = _SRC_ROOT / "mcp" / "responses.py"

_CATEGORY = "AUTHORIZATION_DENIED"


def _names_denial_category(node: ast.expr) -> bool:
    """True if ``node`` is an ``ErrorCategory.AUTHORIZATION_DENIED`` attribute access."""
    return isinstance(node, ast.Attribute) and node.attr == _CATEGORY


class _DenialCallVisitor(ast.NodeVisitor):
    """Collect the line of every call that passes the denial category as an argument."""

    def __init__(self) -> None:
        self.hits: list[int] = []

    def visit_Call(self, node: ast.Call) -> None:
        arguments = list(node.args) + [kw.value for kw in node.keywords]
        if any(_names_denial_category(arg) for arg in arguments):
            self.hits.append(node.lineno)
        self.generic_visit(node)


def test_denial_envelopes_are_built_only_by_tool_response_denied() -> None:
    """No module hand-rolls an ``authorization_denied`` envelope (#1596, ADR-0471)."""
    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path == _ALLOWED:
            continue
        visitor = _DenialCallVisitor()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        offenders += [
            f"{path.relative_to(_SRC_ROOT.parents[1])}:{line}" for line in sorted(visitor.hits)
        ]
    assert not offenders, (
        "an authorization_denied envelope is being built outside ToolResponse.denied, which "
        "lets it name the tool that denied the caller (#1596); use ToolResponse.denied "
        f"instead at: {offenders}"
    )


def test_denial_breadcrumb_is_public_and_registered() -> None:
    """The fixed breadcrumb names live, public tools the denied caller can actually invoke."""
    registry = PUBLIC_TOOLS | CLASSIFIED_TOOLS
    named = set(DENIAL_NEXT_ACTIONS)
    assert named <= registry, f"denial breadcrumb names no registered tool: {named - registry}"
    assert named <= PUBLIC_TOOLS, (
        "a denial breadcrumb must be invokable by the caller that was just denied, so every "
        f"entry must be public; gated entries: {sorted(named - PUBLIC_TOOLS)}"
    )


def test_a_denial_can_never_name_a_gated_tool() -> None:
    """A denial envelope names no gated tool — so it can never name its own denier.

    Only a gated tool can deny (a public tool enforces no role), so a breadcrumb disjoint from
    ``CLASSIFIED_TOOLS`` is disjoint from the set of possible deniers.
    """
    named = set(ToolResponse.denied("any-object").suggested_next_actions)
    assert not named & CLASSIFIED_TOOLS, (
        f"denial envelope names a gated tool the caller may not hold: {sorted(named)}"
    )


# ---------------------------------------------------------------------------
# The missing-grant disclosure (ADR-0490)
# ---------------------------------------------------------------------------
#
# ADR-0490 lets a denial name the grant the caller lacks, under `data.missing_roles`, while
# ADR-0123's suppressed `detail` stays the bare "access denied" constant. What makes that
# disclosure acceptable is that the vocabulary is *closed*: an unprivileged caller probing the
# whole surface learns a fixed, auditable set of role tokens and nothing else. These guards pin
# the two ways that bound could rot — the vocabulary opening up, and a call site writing the key
# by hand with a string the type system never saw.

# Every module under `src/kdive/` that builds a denial WITHOUT naming a missing role, with the
# count of such calls and why no role belongs there. A new role-less denial is a deliberate diff
# here, so "I forgot the role" cannot ship silently.
#
# A per-plane helper that takes its role as a *required* argument and forwards it (the images and
# resources `denied` helpers, `host_ops._denied`, and the dispatch boundary's `_denied_result`) is
# not listed: it always passes the keyword, and the requirement lands on its callers, which cannot
# omit it. `_denied_result` joined them with ADR-0486, which routed both of the boundary's arms
# through one constructor — its `ProjectMembershipDenied` arm passes an explicit `None`, a stated
# "no role would have helped" rather than an omitted argument.
_ROLELESS_DENIALS: dict[str, tuple[int, str]] = {
    "mcp/tools/_common.py": (
        1,
        "authz_denied speaks ADR-0129's `missing_checks`; a role is only one of its factors",
    ),
    "mcp/tools/accounting/reports.py": (
        1,
        "non-member arm only (RoleDenied is re-raised so the boundary still audits it, "
        "ADR-0493); naming a role would confirm the caller-named project exists",
    ),
    "mcp/tools/jobs.py": (
        1,
        "non-member arm only (RoleDenied is re-raised); naming a role would confirm the project",
    ),
    "mcp/tools/ops/audit/audit.py": (
        1,
        "non-member arm only (RoleDenied is re-raised); naming a role would confirm the project",
    ),
    "mcp/tools/reports/generate.py": (
        1,
        "the non-member arm of the granted-set branch; the RoleDenied arm above does name it",
    ),
}

_DENIED = "denied"
_MISSING_ROLES_KWARG = "missing_roles"


class _DeniedCallVisitor(ast.NodeVisitor):
    """Collect `ToolResponse.denied(...)` calls that name no missing role."""

    def __init__(self) -> None:
        self.roleless: list[int] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == _DENIED:
            named = {kw.arg for kw in node.keywords}
            if _MISSING_ROLES_KWARG not in named:
                self.roleless.append(node.lineno)
        self.generic_visit(node)


def _module_key(path: Path) -> str:
    return path.relative_to(_SRC_ROOT).as_posix()


def test_role_less_denials_are_the_documented_set() -> None:
    """A denial that names no missing role is one of the cases ADR-0490 says has none."""
    found: dict[str, int] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path == _ALLOWED:
            continue
        visitor = _DeniedCallVisitor()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        if visitor.roleless:
            found[_module_key(path)] = len(visitor.roleless)
    expected = {module: count for module, (count, _) in _ROLELESS_DENIALS.items()}
    assert found == expected, (
        "a denial builds an envelope without naming the grant the caller lacks (ADR-0490). "
        "Pass `missing_roles=` at the new site, or add it here with the reason no role applies. "
        f"expected {expected}, found {found}"
    )


_KEY_CONSTANT = "MISSING_ROLES_KEY"


def _key_alias_names(tree: ast.Module) -> set[str]:
    """Every local name in ``tree`` bound to `responses.MISSING_ROLES_KEY`.

    `responses.py` exports the constant publicly, so matching the string literal alone is not
    enough: ``data={MISSING_ROLES_KEY: [...]}`` is an `ast.Name`, and
    ``from kdive.mcp.responses import MISSING_ROLES_KEY as _K`` renames it again. Resolving the
    aliases is what makes this guard cover the key rather than one spelling of it.
    """
    aliases = {_KEY_CONSTANT}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases |= {a.asname or a.name for a in node.names if a.name == _KEY_CONSTANT}
        elif isinstance(node, ast.Assign) and _names_the_key(node.value, aliases):
            aliases |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return aliases


def _names_the_key(node: ast.expr, aliases: set[str]) -> bool:
    """True if ``node`` is the key — as a literal, a bare name, or an attribute access."""
    if isinstance(node, ast.Constant):
        return node.value == MISSING_ROLES_KEY
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Attribute):
        return node.attr in aliases
    return False


def test_missing_roles_key_is_written_only_by_the_denial_constructor() -> None:
    """No module names the `missing_roles` key at all, however it spells it.

    The runtime check in `ToolResponse.denied` (it raises when `data` carries the key) is the
    real defence; this guard is the backstop that keeps the key from being *mentioned* outside
    the constructor at all. It resolves the exported `MISSING_ROLES_KEY` constant and any alias
    of it, not just the string literal — matching literals alone left
    ``data={MISSING_ROLES_KEY: ["superuser"]}`` sailing through, using a name this very module
    hands out.
    """
    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path == _ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _key_alias_names(tree)
        offenders += [
            f"{_module_key(path)}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.expr) and _names_the_key(node, aliases)
        ]
    assert not offenders, (
        f"the {MISSING_ROLES_KEY!r} key must only be written by ToolResponse.denied, whose "
        f"typed `missing_roles` argument keeps the disclosed vocabulary closed: {offenders}"
    )


# The per-plane denial helpers that forward a caller-supplied role, as
# (module, function, parameter). `test_role_less_denials_are_the_documented_set` exempts them
# because they always pass `missing_roles=`, but that exemption is only sound while the role is
# genuinely *required* and genuinely *forwarded*: giving the parameter a default, or hardcoding
# an empty list, would let a caller silently drop the role with both other guards green.
_ROLE_FORWARDING_HELPERS: tuple[tuple[str, str, str], ...] = (
    ("mcp/middleware/denial_audit.py", "_denied_result", "missing"),
    ("mcp/tools/ops/images/_common.py", "denied", "role"),
    ("mcp/tools/ops/resources/_common.py", "denied", "role"),
    ("mcp/tools/ops/resources/host_ops.py", "_denied", "role"),
)


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"helper {name!r} not found; update _ROLE_FORWARDING_HELPERS")


def test_role_forwarding_helpers_cannot_default_their_role_away() -> None:
    """Each exempted helper takes its role as a required argument and forwards that argument."""
    problems: list[str] = []
    for module, func_name, param in _ROLE_FORWARDING_HELPERS:
        tree = ast.parse((_SRC_ROOT / module).read_text(encoding="utf-8"))
        func = _function(tree, func_name)
        positional = func.args.posonlyargs + func.args.args
        names = [a.arg for a in positional]
        if param not in names:
            problems.append(f"{module}:{func_name} has no {param!r} parameter")
            continue
        # Defaults bind to the *tail* of the positional list, so a parameter is required iff its
        # index sits before the first defaulted one.
        first_defaulted = len(names) - len(func.args.defaults)
        if names.index(param) >= first_defaulted:
            problems.append(
                f"{module}:{func_name} gives {param!r} a default, so callers may omit it"
            )
        forwards = any(
            isinstance(node, ast.Name) and node.id == param
            for call in ast.walk(func)
            if isinstance(call, ast.Call)
            for kw in call.keywords
            if kw.arg == _MISSING_ROLES_KWARG
            for node in ast.walk(kw.value)
        )
        if not forwards:
            problems.append(f"{module}:{func_name} does not forward {param!r} into missing_roles")
    assert not problems, (
        "a denial helper exempted from the role-less-site guard no longer requires and forwards "
        f"the role that exemption assumes (ADR-0490): {problems}"
    )


def test_missing_role_vocabulary_is_closed_and_unambiguous() -> None:
    """The wire vocabulary is exactly the two live enforcement enums, with disjoint values.

    Disjointness is what lets one flat ``missing_roles`` list carry both tiers: ``"admin"`` can
    only ever be the project role and ``"platform_admin"`` only ever the platform one. A future
    ``Role.PLATFORM_ADMIN`` (or a ``PlatformRole.ADMIN``) would make a token mean two things.
    """
    project = {role.value for role in Role}
    platform = {role.value for role in PlatformRole}
    assert not project & platform, (
        f"a missing_roles token would be ambiguous across the two role tiers: {project & platform}"
    )
    emitted = ToolResponse.denied("any-object", missing_roles=[*Role, *PlatformRole]).data[
        MISSING_ROLES_KEY
    ]
    assert emitted == sorted(project | platform)
