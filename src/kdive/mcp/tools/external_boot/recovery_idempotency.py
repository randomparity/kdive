"""Public recovery request replay and fixed server-clock deadlines (ADR-0583)."""

import hashlib
import json
from datetime import timedelta

from psycopg import AsyncConnection

from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import Job
from kdive.jobs import queue
from kdive.jobs.payloads import RecoveryRequestV1
from kdive.mcp.responses import ToolResponse


def recovery_response(job: Job, object_key: str, object_id: str) -> ToolResponse:
    response = ToolResponse.from_job(job)
    data = {**response.data, object_key: object_id}
    recorded = job.payload.get("recovery_request_v1")
    if recorded is not None:
        metadata = RecoveryRequestV1.model_validate(recorded)
        data["recovery_readiness_deadline"] = metadata.readiness_deadline.isoformat().replace(
            "+00:00", "Z"
        )
    return response.model_copy(update={"data": data})


async def recovery_request(
    conn: AsyncConnection,
    *,
    tool: str,
    object_key: str,
    object_id: str,
    arguments: tuple[str, ...],
    idempotency_key: str | None,
) -> tuple[str, RecoveryRequestV1 | None, ToolResponse | None]:
    """Call after authorization, under the System lock, before state-dependent admission."""
    if idempotency_key is not None and (not idempotency_key or len(idempotency_key.encode()) > 255):
        return (
            "",
            None,
            ToolResponse.failure(
                object_id,
                ErrorCategory.CONFIGURATION_ERROR,
                detail="idempotency_key must contain 1 through 255 UTF-8 bytes",
                data={"reason": "invalid_idempotency_key"},
                suggested_next_actions=[tool],
            ),
        )
    encoded = json.dumps([tool, object_id, *arguments], separators=(",", ":")).encode()
    identity = "sha256:" + hashlib.sha256(encoded).hexdigest()
    key_bytes = json.dumps([tool, object_id, idempotency_key or identity]).encode()
    dedup_key = "external-boot-request:" + hashlib.sha256(key_bytes).hexdigest()
    existing = await queue.get_by_dedup_key(conn, dedup_key)
    if existing is not None:
        metadata = RecoveryRequestV1.model_validate(existing.payload.get("recovery_request_v1"))
        if metadata.request_identity != identity:
            return (
                dedup_key,
                None,
                ToolResponse.failure(
                    object_id,
                    ErrorCategory.CONFLICT,
                    detail="idempotency_key is already bound to different recovery inputs",
                    data={"reason": "idempotency_key_conflict"},
                    suggested_next_actions=[tool],
                ),
            )
        return dedup_key, metadata, recovery_response(existing, object_key, object_id)
    row = await (await conn.execute("SELECT clock_timestamp()")).fetchone()
    if row is None:
        raise RuntimeError("recovery request could not read the server clock")
    metadata = RecoveryRequestV1(
        request_identity=identity, readiness_deadline=row[0] + timedelta(minutes=5)
    )
    return dedup_key, metadata, None
