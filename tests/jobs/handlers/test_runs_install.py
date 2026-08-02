"""Coverage anchor for the split install run handler module."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from kdive.domain.capacity.state import JobState
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.runs import install as runs_install
from kdive.jobs.handlers.runs import registrar as runs
from kdive.jobs.payloads import PayloadValidationError


def test_install_handler_is_exported_through_runs_facade() -> None:
    assert runs.install_handler is runs_install.install_handler


@pytest.mark.parametrize("cmdline", ["x" * 4097, "panic=1\x00"])
def test_install_handler_rejects_unsafe_persisted_cmdline(cmdline: str) -> None:
    now = datetime.now(UTC)
    job = Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.INSTALL,
        payload={"run_id": str(uuid4()), "cmdline": cmdline},
        state=JobState.RUNNING,
        max_attempts=3,
        authorizing={"principal": "p", "project": "proj", "agent_session": None},
        dedup_key="install",
    )

    with pytest.raises(PayloadValidationError, match="cmdline_(too_long|not_printable)"):
        runs_install._install_payload_context(job)
