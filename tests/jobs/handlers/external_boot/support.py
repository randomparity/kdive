"""Shared builders for the external-boot handler package tests.

A ``*_support.py`` module rather than helpers in ``conftest.py``, matching
``tests/db/external_boot_authority_support.py`` and ``tests/mcp/systems_support.py``: conftest is
loaded by pytest's own machinery, and importing it as an ordinary module would load it twice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from kdive.domain.capacity.state import JobState
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.payloads import EXTERNAL_BOOT_AUTHORITY_MARKER_KEY

DIGEST = "sha256:" + "a" * 64


def marker_fields(
    *,
    activation_id: UUID | None = None,
    run_id: UUID | None = None,
    system_id: UUID | None = None,
    purpose: str = "activate",
    operation: str = "activate",
    plan_identity: str = DIGEST,
    provider_kind: str = "local-libvirt",
    authority_instance: str = "provider-1",
) -> dict[str, Any]:
    """Build the marker's JSON fields; every id defaults to a fresh one."""
    return {
        "activation_id": str(activation_id or uuid4()),
        "run_id": str(run_id or uuid4()),
        "system_id": str(system_id or uuid4()),
        "plan_identity": plan_identity,
        "purpose": purpose,
        "provider_kind": provider_kind,
        "authority_instance": authority_instance,
        "operation": operation,
        "operation_identity": f"{operation}-1",
    }


def build_job(kind: JobKind, payload: dict[str, Any]) -> Job:
    """Build an in-memory ``Job`` carrying ``payload`` verbatim, with no database round trip."""
    now = datetime.now(UTC)
    return Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=kind,
        payload=payload,
        state=JobState.RUNNING,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "kernel-team"},
        dedup_key=f"{uuid4()}:{kind.value}",
    )


def marked_job(
    operation: str,
    *,
    purpose: str | None = None,
    run_id: UUID | None = None,
    system_id: UUID | None = None,
    **marker_overrides: Any,
) -> Job:
    """Build a marked ``boot`` or ``teardown`` job for ``operation``.

    ``purpose`` defaults to the operation's own name for the five operations that carry one, and
    to ``release`` for ``cleanup`` — the pairing ``_PURPOSE_OPERATIONS`` admits.
    """
    resolved_purpose = purpose or ("release" if operation == "cleanup" else operation)
    run_id = run_id or uuid4()
    system_id = system_id or uuid4()
    fields = marker_fields(
        run_id=run_id,
        system_id=system_id,
        purpose=resolved_purpose,
        operation=operation,
        **marker_overrides,
    )
    if resolved_purpose == "teardown":
        return build_job(
            JobKind.TEARDOWN,
            {"system_id": str(system_id), EXTERNAL_BOOT_AUTHORITY_MARKER_KEY: fields},
        )
    return build_job(
        JobKind.BOOT, {"run_id": str(run_id), EXTERNAL_BOOT_AUTHORITY_MARKER_KEY: fields}
    )
