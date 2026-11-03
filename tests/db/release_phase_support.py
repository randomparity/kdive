"""Seeding and calls shared by the release-phase connected tests (ADR-0614, ADR-0714)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, LiteralString

import psycopg
from psycopg.rows import dict_row

from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityTakeoverRequestV1,
)
from tests.jobs.handlers.external_boot.seeding import AUTHORITY_INSTANCE


async def scalar(
    conn: psycopg.AsyncConnection, sql: LiteralString, args: tuple[object, ...]
) -> object:
    row = await (await conn.execute(sql, args)).fetchone()
    assert row is not None
    return row[0]


async def allocate_release_root(
    conn: psycopg.AsyncConnection, case: Any, *, credential: str, attempt: int
) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM allocate_external_boot_authority("
            "sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                credential,
                case.job_id,
                attempt,
                case.vehicle.activation_id,
                case.vehicle.run_id,
                case.vehicle.system_id,
                case.vehicle.plan_identity,
                "release",
                "local-libvirt",
                AUTHORITY_INSTANCE,
                case.marker["operation_identity"],
            ),
        )
        allocated = await cur.fetchone()
    assert allocated is not None
    assert allocated["status"] == "allocated"
    return allocated


async def begin_release_recovery(
    conn: psycopg.AsyncConnection,
    *,
    credential: str,
    job_id: object,
    attempt: int,
    authority_id: object,
    generation: object,
    attempt_id: object,
    deadline: datetime | None = None,
) -> str:
    return str(
        await scalar(
            conn,
            "SELECT begin_external_boot_derived_release_recovery("
            "sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,%s,%s)",
            (
                credential,
                job_id,
                attempt,
                authority_id,
                generation,
                attempt_id,
                deadline or datetime.now(UTC) + timedelta(minutes=5),
            ),
        )
    )


async def acknowledge_release_root(
    conn: psycopg.AsyncConnection,
    case: Any,
    authority: dict[str, Any],
    *,
    worker_incarnation: str,
) -> None:
    assert (
        await scalar(
            conn,
            "SELECT status FROM acknowledge_external_boot_authority("
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                authority["authority_id"],
                authority["generation"],
                case.allocation_id,
                case.vehicle.activation_id,
                case.vehicle.run_id,
                case.vehicle.system_id,
                case.vehicle.plan_identity,
                case.job_id,
                authority["generation"],
                "release",
                "local-libvirt",
                AUTHORITY_INSTANCE,
                worker_incarnation,
                "release",
                case.marker["operation_identity"],
                authority["operation_digest"],
                1,
                "sha256:" + "b" * 64,
                "sha256:" + "c" * 64,
            ),
        )
        == "applied"
    )


async def release_phase(
    conn: psycopg.AsyncConnection,
    case: Any,
    authority: dict[str, Any],
    worker_incarnation: str,
    operation: str,
) -> dict[str, Any]:
    root = {
        "authority_id": str(authority["authority_id"]),
        "generation": authority["generation"],
        "system_id": str(case.vehicle.system_id),
        "activation_id": str(case.vehicle.activation_id),
        "run_id": str(case.vehicle.run_id),
        "plan_identity": case.vehicle.plan_identity,
        "provider_kind": "local-libvirt",
        "authority_instance": AUTHORITY_INSTANCE,
        "worker_incarnation": worker_incarnation,
        "root_operation_identity": case.marker["operation_identity"],
        "root_operation_digest": authority["operation_digest"],
    }
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM derive_external_boot_release_phase_binding(%s::jsonb,%s)",
            (json.dumps(root), operation),
        )
        phase = await cur.fetchone()
    assert phase is not None
    return phase


class SourceAuthorityAdapter:
    async def commit(
        self, request: AuthorityMutationRequestV1, context: object
    ) -> AuthorityObservationV1:
        del context
        return await self.observe(request)

    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        return AuthorityObservationV1(
            observation_id=request.attempt_id,
            category="source",
            composite_state="sha256:" + "a" * 64,
        )


def release_takeover(case: Any, authority: dict[str, Any]) -> AuthorityTakeoverRequestV1:
    return AuthorityTakeoverRequestV1(
        authority_id=authority["authority_id"],
        generation=authority["generation"],
        system_id=case.vehicle.system_id,
        activation_id=case.vehicle.activation_id,
        run_id=case.vehicle.run_id,
        plan_identity=case.vehicle.plan_identity,
        purpose="release",
        operation="release",
        provider_kind="local-libvirt",
        authority_instance=AUTHORITY_INSTANCE,
        operation_identity=case.marker["operation_identity"],
        operation_digest=authority["operation_digest"],
    )
