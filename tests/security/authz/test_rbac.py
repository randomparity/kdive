"""Tests for project-scoped RBAC (ADR-0006, ADR-0020)."""

from __future__ import annotations

from typing import Any, cast

import pytest

from kdive.mcp.auth import AuthError, RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    Role,
    RoleDenied,
    platform_roles_from_claims,
    require_platform_role,
    require_role,
    roles_from_claims,
)


def _ctx(
    *, projects: tuple[str, ...] = ("proj",), roles: dict[str, Role] | None = None
) -> RequestContext:
    return RequestContext(
        principal="alice", agent_session=None, projects=projects, roles=roles or {}
    )


def test_roles_from_claims_absent_is_empty() -> None:
    assert roles_from_claims({"sub": "alice"}) == {}


def test_roles_from_claims_parses_map() -> None:
    assert roles_from_claims({"roles": {"a": "admin", "b": "operator"}}) == {
        "a": Role.ADMIN,
        "b": Role.OPERATOR,
    }


def test_roles_from_claims_rejects_non_object() -> None:
    with pytest.raises(AuthError, match=r"^roles claim is not an object$"):
        roles_from_claims({"roles": ["admin"]})


def test_roles_from_claims_rejects_unknown_role() -> None:
    with pytest.raises(AuthError):
        roles_from_claims({"roles": {"a": "superadmin"}})


def test_roles_from_claims_rejects_non_string_value() -> None:
    with pytest.raises(AuthError, match=r"^roles claim value for project 'a' is not a string$"):
        roles_from_claims({"roles": {"a": 1}})


def test_roles_from_claims_rejects_empty_project_key() -> None:
    with pytest.raises(AuthError, match=r"^roles claim project key '' is not a non-empty string$"):
        roles_from_claims({"roles": {"": "admin"}})


def test_roles_from_claims_rejects_non_string_project_key() -> None:
    with pytest.raises(AuthError, match=r"^roles claim project key 7 is not a non-empty string$"):
        roles_from_claims({"roles": cast(Any, {7: "admin"})})


def test_require_role_admin_satisfies_operator() -> None:
    require_role(_ctx(roles={"proj": Role.ADMIN}), "proj", Role.OPERATOR)


def test_require_role_exact_match_ok() -> None:
    require_role(_ctx(roles={"proj": Role.OPERATOR}), "proj", Role.OPERATOR)


def test_require_role_too_low_raises_role_denied() -> None:
    # The member-over-reach (rank-below) site raises the dedicated RoleDenied subclass,
    # the discriminator the dispatch boundary catches to audit the denial.
    with pytest.raises(RoleDenied):
        require_role(_ctx(roles={"proj": Role.OPERATOR}), "proj", Role.ADMIN)


def test_role_denied_carries_project_principal_and_roles() -> None:
    # The exception is the only carrier of project at the dispatch boundary (object-
    # resolving tools resolve project from the row, not the call args).
    ctx = _ctx(projects=("proj",), roles={"proj": Role.OPERATOR})
    with pytest.raises(RoleDenied) as excinfo:
        require_role(ctx, "proj", Role.ADMIN)
    denial = excinfo.value
    assert denial.project == "proj"
    assert denial.principal == "alice"
    assert denial.held is Role.OPERATOR
    assert denial.required is Role.ADMIN
    assert str(denial) == "'alice' needs role 'admin' on project 'proj'; holds 'operator'"


def test_role_denied_message_renders_none_when_no_role_held() -> None:
    # A member with no per-project role renders the held slot as the literal "none"
    # rather than a role value (the human-readable denial message, ADR-0062 §5).
    ctx = _ctx(projects=("proj",), roles={})
    with pytest.raises(RoleDenied) as excinfo:
        require_role(ctx, "proj", Role.VIEWER)
    assert str(excinfo.value) == "'alice' needs role 'viewer' on project 'proj'; holds 'none'"


def test_require_role_member_without_role_raises_role_denied() -> None:
    # The common token shape: granted membership, no per-project role. Membership is
    # held, so this is the rank-below (member-over-reach) site → RoleDenied, held=None.
    ctx = _ctx(projects=("proj",), roles={})
    with pytest.raises(RoleDenied) as excinfo:
        require_role(ctx, "proj", Role.VIEWER)
    assert excinfo.value.held is None
    assert excinfo.value.project == "proj"


def test_require_role_not_a_member_raises_base_authorization_error_not_role_denied() -> None:
    # The non-member site keeps the base AuthorizationError so the dispatch boundary
    # (which catches RoleDenied specifically) never audits it — no write-amplification.
    with pytest.raises(AuthorizationError) as excinfo:
        require_role(_ctx(projects=("other",), roles={"proj": Role.ADMIN}), "proj", Role.VIEWER)
    assert not isinstance(excinfo.value, RoleDenied)
    assert str(excinfo.value) == "'alice' is not a member of project 'proj'"


def test_role_denied_is_authorization_error_subclass() -> None:
    # Subclass relationship is load-bearing: the gate's `except AuthorizationError`
    # still catches a rank-below denial from require_role.
    assert issubclass(RoleDenied, AuthorizationError)


def test_platform_roles_from_claims_rejects_non_array_message() -> None:
    # The wrong-shape rejection carries the array-specific message so a misconfigured
    # token surfaces an actionable error (fail closed, mirrors roles_from_claims).
    with pytest.raises(AuthError, match=r"^platform_roles claim is not an array$"):
        platform_roles_from_claims({"platform_roles": {"platform_auditor": True}})


def test_platform_roles_from_claims_rejects_non_string_entry_message() -> None:
    with pytest.raises(AuthError, match=r"^platform_roles claim entry 1 is not a string$"):
        platform_roles_from_claims({"platform_roles": [1]})


def test_require_platform_role_denied_message_lists_held_roles() -> None:
    # The platform-tier denial message names the required role and the sorted set held,
    # so an operator-only token denied an auditor operation gets an actionable error.
    ctx = RequestContext(
        principal="alice",
        agent_session=None,
        projects=(),
        platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}),
    )
    with pytest.raises(AuthorizationError) as excinfo:
        require_platform_role(ctx, PlatformRole.PLATFORM_AUDITOR)
    assert str(excinfo.value) == (
        "'alice' needs platform role 'platform_auditor'; holds ['platform_operator']"
    )


def test_request_context_with_roles_is_hashable() -> None:
    ctx = _ctx(roles={"proj": Role.ADMIN})
    assert hash(ctx) == hash(ctx)  # does not raise despite the dict field
    assert ctx.roles["proj"] is Role.ADMIN
