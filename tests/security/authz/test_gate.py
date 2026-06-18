"""Tests for the two-check destructive-op gate (ADR-0130, refines ADR-0006/0020)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from kdive.domain.jobs import JobKind
from kdive.domain.lifecycle import Allocation
from kdive.domain.state import AllocationState
from kdive.mcp.auth import RequestContext
from kdive.security.authz.gate import DestructiveOp, DestructiveOpDenied, assert_destructive_allowed
from kdive.security.authz.rbac import Role

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(role: Role) -> RequestContext:
    return RequestContext(
        principal="alice", agent_session=None, projects=("proj",), roles={"proj": role}
    )


def _allocation() -> Allocation:
    return Allocation.model_validate(
        dict(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=uuid4(),
            state=AllocationState.ACTIVE,
        )
    )


def _op(opt_in: bool = True) -> DestructiveOp:
    return DestructiveOp(kind=JobKind.FORCE_CRASH, profile_opt_in=opt_in)


def test_role_and_opt_in_present_is_allowed() -> None:
    assert assert_destructive_allowed(_ctx(Role.ADMIN), _allocation(), _op(True)) is None


def test_not_admin_denied() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(_ctx(Role.OPERATOR), _allocation(), _op(True))
    assert exc.value.missing == ["admin_role"]


def test_opt_in_false_denied() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(_ctx(Role.ADMIN), _allocation(), _op(False))
    assert exc.value.missing == ["profile_opt_in"]


def test_opt_in_defaults_false() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(
            _ctx(Role.ADMIN), _allocation(), DestructiveOp(kind=JobKind.FORCE_CRASH)
        )
    assert exc.value.missing == ["profile_opt_in"]


def test_both_absent_lists_role_then_opt_in() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(_ctx(Role.OPERATOR), _allocation(), _op(False))
    assert exc.value.missing == ["admin_role", "profile_opt_in"]


def test_operator_required_role_allows_operator() -> None:
    # Reprovision's role factor is operator (ADR-0038): an operator with opt-in passes.
    assert (
        assert_destructive_allowed(
            _ctx(Role.OPERATOR),
            _allocation(),
            DestructiveOp(kind=JobKind.REPROVISION, profile_opt_in=True),
            required_role=Role.OPERATOR,
        )
        is None
    )


def test_operator_required_role_still_denies_viewer() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(
            _ctx(Role.VIEWER),
            _allocation(),
            DestructiveOp(kind=JobKind.REPROVISION, profile_opt_in=True),
            required_role=Role.OPERATOR,
        )
    assert exc.value.missing == ["operator_role"]


def test_required_role_defaults_to_admin() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(_ctx(Role.OPERATOR), _allocation(), _op(True))
    assert exc.value.missing == ["admin_role"]
