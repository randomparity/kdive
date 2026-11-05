"""The `platform_admin` entry to stranded activation settlement (ADR-0816)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection

from kdive.db.external_boot_activations import SettlementStatus
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole
from kdive.services.external_boot import settlement

_ADMIN = RequestContext(
    principal="admin",
    agent_session="session",
    projects=(),
    platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
)
_AUDITOR = RequestContext(
    principal="auditor",
    agent_session=None,
    projects=(),
    platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}),
)


class _Repository:
    def __init__(self, status: SettlementStatus) -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    async def settle_stranded(self, conn: object, **values: Any) -> SettlementStatus:
        self.calls.append(values)
        return self.status


def _bind(monkeypatch: pytest.MonkeyPatch, status: SettlementStatus) -> _Repository:
    repository = _Repository(status)
    monkeypatch.setattr(settlement, "_REPOSITORY", repository)
    return repository


async def _call(ctx: RequestContext, reason: str = "stranded by #3138") -> SettlementStatus:
    return await settlement.settle_stranded_activation(
        cast(AsyncConnection, object()),
        ctx,
        activation_id=uuid4(),
        actor="operator-cli",
        reason=reason,
    )


@pytest.mark.parametrize("status", [SettlementStatus.APPLIED, SettlementStatus.REPLAYED])
def test_admin_settles_with_their_identity(
    monkeypatch: pytest.MonkeyPatch, status: SettlementStatus
) -> None:
    repository = _bind(monkeypatch, status)

    assert asyncio.run(_call(_ADMIN)) is status

    call = repository.calls[0]
    assert (call["principal"], call["agent_session"], call["actor"], call["reason"]) == (
        "admin",
        "session",
        "operator-cli",
        "stranded by #3138",
    )


def test_a_lesser_platform_role_never_reaches_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _bind(monkeypatch, SettlementStatus.APPLIED)

    with pytest.raises(AuthorizationError):
        asyncio.run(_call(_AUDITOR))

    assert repository.calls == []


@pytest.mark.parametrize("reason", ["", "  \n", "x" * 1025, "é" * 513])
def test_an_invalid_reason_never_reaches_the_repository(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    repository = _bind(monkeypatch, SettlementStatus.APPLIED)

    with pytest.raises(CategorizedError) as refused:
        asyncio.run(_call(_ADMIN, reason))

    assert refused.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert refused.value.details["reason"] == "reason_required"
    assert repository.calls == []


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (SettlementStatus.NOT_FOUND, ErrorCategory.NOT_FOUND),
        (SettlementStatus.AUTHORITY_OWNED, ErrorCategory.CONFLICT),
        (SettlementStatus.WORKER_EVIDENCE_MISSING, ErrorCategory.CONFLICT),
    ],
)
def test_a_refusal_names_its_closed_reason(
    monkeypatch: pytest.MonkeyPatch, status: SettlementStatus, category: ErrorCategory
) -> None:
    _bind(monkeypatch, status)

    with pytest.raises(CategorizedError) as refused:
        asyncio.run(_call(_ADMIN))

    assert refused.value.category is category
    assert refused.value.details["reason"] == status.value
