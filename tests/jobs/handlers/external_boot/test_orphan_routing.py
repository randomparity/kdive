"""Orphan disposition routes by the System's binding; the serving host owns the rows (#3439)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind, JobState
from kdive.jobs.handlers.external_boot.orphan import resolve_recovery_orphan_handler
from kdive.providers.external_boot_authority.orphan import RecoveryOrphanAuthorityService
from kdive.providers.external_boot_authority.protocol import (
    AuthorityRecoveryOrphanDispositionRequestV1,
    AuthorityRecoveryOrphanDispositionResponseV1,
)
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    AuthorityServiceError,
)

_SYSTEM = uuid4()


class _Sender:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve_recovery_orphan(
        self, request: AuthorityRecoveryOrphanDispositionRequestV1, *, deadline: float
    ) -> AuthorityRecoveryOrphanDispositionResponseV1:
        self.calls += 1
        return AuthorityRecoveryOrphanDispositionResponseV1(
            request_id=request.request_id, disposition="delete", objects=1
        )


class _Resolver:
    def __init__(self, authority: object | None) -> None:
        self._binding = SimpleNamespace(runtime=SimpleNamespace(authority=authority))
        self.systems: list[object] = []

    async def binding_for_system(self, conn: object, system_id: object) -> Any:
        self.systems.append(system_id)
        return self._binding


class _Conn:
    async def execute(self, query: str) -> Any:
        async def fetchone() -> tuple[datetime]:
            return (datetime.now(UTC),)

        return SimpleNamespace(fetchone=fetchone)


def _job() -> Job:
    now = datetime.now(UTC)
    return Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.RESOLVE_RECOVERY_ORPHAN,
        payload={
            "schema": "resolve-recovery-orphan-v1",
            "system_id": str(_SYSTEM),
            "request_id": str(uuid4()),
            "binding_digest": "sha256:" + "0" * 64,
            "recovery_request_v1": {
                "request_identity": "sha256:" + "1" * 64,
                "readiness_deadline": (now + timedelta(minutes=5)).isoformat(),
            },
        },
        state=JobState.RUNNING,
        attempt=1,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "p"},
        dedup_key=str(uuid4()),
    )


@pytest.mark.parametrize("route", ["local-libvirt", "remote-libvirt"])
def test_job_sends_through_the_system_bindings_sender(route: str) -> None:
    asyncio.run(_test_job_sends_through_the_system_bindings_sender(route))


async def _test_job_sends_through_the_system_bindings_sender(route: str) -> None:
    routes = {"local-libvirt": _Sender(), "remote-libvirt": _Sender()}
    resolver = _Resolver(SimpleNamespace(sender=routes[route], missing_route_reason="x"))

    await resolve_recovery_orphan_handler(cast(Any, _Conn()), _job(), resolver=cast(Any, resolver))

    assert {name: sender.calls for name, sender in routes.items()} == {
        name: int(name == route) for name in routes
    }
    assert resolver.systems == [_SYSTEM]


@pytest.mark.parametrize(
    "authority",
    [None, SimpleNamespace(sender=None, missing_route_reason="authority_route_mismatch")],
)
def test_job_without_a_worker_route_is_a_terminal_configuration_error(
    authority: object | None,
) -> None:
    asyncio.run(_test_job_without_a_worker_route_is_a_terminal_configuration_error(authority))


async def _test_job_without_a_worker_route_is_a_terminal_configuration_error(
    authority: object | None,
) -> None:
    with pytest.raises(CategorizedError) as raised:
        await resolve_recovery_orphan_handler(
            cast(Any, _Conn()), _job(), resolver=cast(Any, _Resolver(authority))
        )

    assert raised.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert raised.value.terminal


class _Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    async def execute(self, *args: object) -> None:
        return None

    async def fetchall(self) -> list[dict[str, object]]:
        return self._rows


class _Ports:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"provider I/O {name} before the ownership check")


def _row(instance: str) -> dict[str, object]:
    return {
        "object_id": uuid4(),
        "system_id": _SYSTEM,
        "activation_id": uuid4(),
        "run_id": uuid4(),
        "disposition": "delete",
        "authority_instance": instance,
        "object_kind": "kernel",
        "object_reference": "kernel/a",
        "ownership_digest": "sha256:" + "2" * 64,
        "observed_digest": "sha256:" + "3" * 64,
        "operation_identity": "op",
        "attempt_id": uuid4(),
        "mutation_journal_sequence": 1,
        "mutation_journal_digest": "sha256:" + "4" * 64,
        "reserved_bytes": 1,
        "readiness_deadline": datetime.now(UTC) + timedelta(minutes=5),
    }


def _service(rows: list[dict[str, object]]) -> RecoveryOrphanAuthorityService:
    @asynccontextmanager
    async def connections() -> Any:
        conn = SimpleNamespace(cursor=lambda row_factory: _CursorContext(rows))
        yield conn

    return RecoveryOrphanAuthorityService(
        cast(Any, connections), cast(Any, _Ports()), authority_instance="sys-R1"
    )


class _CursorContext:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._cursor = _Cursor(rows)

    async def __aenter__(self) -> _Cursor:
        return self._cursor

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.mark.parametrize("instances", [["sys-R2"], ["sys-R1", "sys-R2"]], ids=["foreign", "mixed"])
def test_host_refuses_a_foreign_row_before_any_provider_io(instances: list[str]) -> None:
    asyncio.run(_test_host_refuses_a_foreign_row_before_any_provider_io(instances))


async def _test_host_refuses_a_foreign_row_before_any_provider_io(instances: list[str]) -> None:
    service = _service([_row(instance) for instance in instances])
    request = AuthorityRecoveryOrphanDispositionRequestV1(
        request_id=uuid4(), job_id=uuid4(), job_attempt=1
    )

    with pytest.raises(AuthorityServiceError) as raised:
        await service.resolve_recovery_orphan(AuthenticatedPeer("peer"), request)

    assert raised.value.category == "configuration_error"
    assert raised.value.reason == "orphan_row_foreign_authority"


def test_host_accepts_rows_of_its_own_instance_past_the_ownership_check() -> None:
    asyncio.run(_test_host_accepts_rows_of_its_own_instance_past_the_ownership_check())


async def _test_host_accepts_rows_of_its_own_instance_past_the_ownership_check() -> None:
    service = _service([_row("sys-R1")])
    request = AuthorityRecoveryOrphanDispositionRequestV1(
        request_id=uuid4(), job_id=uuid4(), job_attempt=1
    )

    selections = await service._selection(AuthenticatedPeer("peer"), request)  # noqa: SLF001

    assert [selection.authority_instance for selection in selections] == ["sys-R1"]
