"""Job builders shared by worker telemetry tests."""

from datetime import UTC, datetime
from uuid import uuid4

from kdive.domain.capacity.state import JobState
from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_job(state: JobState, category: ErrorCategory | None = None) -> Job:
    """Build a worker job with stable timestamps and unique identity fields."""
    return Job(
        id=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        kind=JobKind.BUILD,
        payload={},
        state=state,
        max_attempts=3,
        error_category=category,
        authorizing={"principal": "alice", "agent_session": None, "project": "proj"},
        dedup_key=str(uuid4()),
    )
