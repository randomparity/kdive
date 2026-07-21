"""Direct unit tests for the Investigation open/close service guards.

These cover the authorization and validation branches of ``open_investigation_record`` that
reject before the row insert touches the connection. The insert and the Postgres-locked close
transition are covered by the service-level (PG-backed) suite.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from psycopg import AsyncConnection

from kdive.security.authz.context import RequestContext
from kdive.security.authz.errors import ProjectMembershipDenied
from kdive.security.authz.rbac import Role, RoleDenied
from kdive.services.investigations.common import (
    SUMMARY_MAX,
    ExternalRefInput,
    InvestigationErrorReason,
    InvestigationServiceError,
    require_summary,
)
from kdive.services.investigations.lifecycle import open_investigation_record

_CONN = cast(AsyncConnection, object())


def _ctx(role: Role = Role.CONTRIBUTOR) -> RequestContext:
    return RequestContext(
        principal="p",
        agent_session="s",
        projects=("proj",),
        roles={"proj": role},
    )


def _open(
    ctx: RequestContext,
    *,
    project: str,
    title: str,
    external_refs: list[ExternalRefInput] | None = None,
) -> object:
    return asyncio.run(
        open_investigation_record(
            _CONN, ctx, project=project, title=title, external_refs=external_refs
        )
    )


def test_open_rejects_a_non_member_project() -> None:
    with pytest.raises(ProjectMembershipDenied):
        _open(_ctx(), project="other", title="t")


def test_open_requires_contributor_role() -> None:
    with pytest.raises(RoleDenied):
        _open(_ctx(role=Role.VIEWER), project="proj", title="t")


def test_open_rejects_out_of_bounds_title() -> None:
    with pytest.raises(InvestigationServiceError) as err:
        _open(_ctx(), project="proj", title="x" * 201)
    assert err.value.reason is InvestigationErrorReason.INVALID_TEXT
    assert err.value.object_id == "proj"


def test_require_summary_returns_valid_summary() -> None:
    assert require_summary("inv-1", "found the bug") == "found the bug"
    assert require_summary("inv-1", "x" * SUMMARY_MAX) == "x" * SUMMARY_MAX


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_require_summary_rejects_blank(blank: str) -> None:
    with pytest.raises(InvestigationServiceError) as err:
        require_summary("inv-1", blank)
    assert err.value.reason is InvestigationErrorReason.MISSING_REQUIRED_FIELD
    assert err.value.object_id == "inv-1"


def test_require_summary_rejects_oversized() -> None:
    with pytest.raises(InvestigationServiceError) as err:
        require_summary("inv-1", "x" * (SUMMARY_MAX + 1))
    assert err.value.reason is InvestigationErrorReason.INVALID_TEXT


def test_open_rejects_malformed_external_refs() -> None:
    with pytest.raises(InvestigationServiceError) as err:
        _open(
            _ctx(),
            project="proj",
            title="t",
            external_refs=cast(list[ExternalRefInput], [{"tracker": "bz"}]),
        )
    assert err.value.reason is InvestigationErrorReason.INVALID_EXTERNAL_REF
    assert err.value.object_id == "proj"
